"""The tools' outputs: masked, capped, carrying the ids a model may cite; bad calls refused."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from deltasleuth.agent import prompt
from deltasleuth.agent.prompt import frame
from deltasleuth.agent.tools import (
    DEFAULT_FINDINGS,
    MAX_DOC_BYTES,
    MAX_FINDINGS,
    TOOLS,
    Scope,
    ToolError,
    _top,
    run_tool,
    tools_for,
)
from deltasleuth.approvals import ApprovalRequired
from deltasleuth.auth.principal import expand
from deltasleuth.connectors.tables import plan
from deltasleuth.core import jsonio
from deltasleuth.core.spec import TargetRegistry
from deltasleuth.core.types import DatabaseTarget, Target
from deltasleuth.llm.client import DEFAULT_BYTES_PER_TOKEN
from deltasleuth.llm.profiles import Budgets
from deltasleuth.reports import accuracy
from deltasleuth.reports.csv import parse_csv, render_csv
from deltasleuth.store import repo
from tests.conftest import TESTER
from tests.unit.connectors.test_postgres import compiled
from tests.unit.store.four_eyes import person

from .conftest import RAW_EMAIL, RAW_KEY, TARGETS, seed


@pytest.mark.db
async def test_get_observations_gives_masked_capped_sides_and_their_ids(make_ctx: Any) -> None:
    ctx = make_ctx(None)  # the tools need no model
    seeded = await seed(ctx, note_b="n" * MAX_DOC_BYTES)
    data = await run_tool(ctx, Scope(seeded.run_id, TESTER), "get_observations", {"key": seeded.key})

    a, b = data["side_a"], data["side_b"]
    assert (a["state"], a["truncated"], b["truncated"]) == ("COMPLETE", False, True)
    assert (
        json.loads(a["canonical"])["email"].startswith("tok:email:") and len(b["canonical"]) == MAX_DOC_BYTES
    )
    assert [a["obs_id"], b["obs_id"]] == [data["result"]["obs_a_id"], data["result"]["obs_b_id"]]
    assert data["cycle"] == 1  # the cycle read
    assert {o["obs_id"] for o in data["observations"]} == {a["obs_id"], b["obs_id"]}
    assert {f["finding_id"] for f in data["findings"]} == set(seeded.findings.values())
    shown = frame("get_observations", data)
    assert RAW_KEY not in shown and RAW_EMAIL not in shown


@pytest.mark.db
async def test_a_missing_blob_is_a_tool_error_the_model_reads(make_ctx: Any) -> None:
    ctx = make_ctx(None)
    seeded = await seed(ctx)
    async with ctx.pool.connection() as conn:
        rows = await repo.list_observations(conn, seeded.run_id)
    for row in rows:  # purged or lost
        digest = row["content_hash"].removeprefix("hk1:")
        (ctx.settings.evidence_dir / "blobs" / "hk1" / digest[:2] / f"{digest}.dse").unlink(missing_ok=True)
    with pytest.raises(ToolError, match=r"^evidence of O-[0-9a-f]{16} is missing \(purged or lost\)$"):
        await run_tool(ctx, Scope(seeded.run_id, TESTER), "get_observations", {"key": seeded.key})


@pytest.mark.db
async def test_a_run_stays_readable_when_its_target_is_removed(make_ctx: Any) -> None:
    seeded = await seed(make_ctx(None))
    ctx = replace(make_ctx(None), targets=TargetRegistry({"svc-a": TARGETS.get("svc-a")}))

    data = await run_tool(ctx, Scope(seeded.run_id, TESTER), "get_observations", {"key": seeded.key})

    assert data["side_b"]["state"] == "COMPLETE"  # observed as the run's targets declared


@pytest.mark.db
async def test_a_wide_key_with_long_values_fits_the_call_budget(make_ctx: Any) -> None:
    ctx = make_ctx(None)
    seeded = await seed(ctx, note_b="n" * 100_000, wide=60)
    scope, key = Scope(seeded.run_id, TESTER), {"key": seeded.key}

    observed = await run_tool(ctx, scope, "get_observations", key)
    found = await run_tool(ctx, scope, "get_findings", key | {"limit": MAX_FINDINGS})

    assert (len(observed["findings"]), observed["more_findings"]) == (DEFAULT_FINDINGS, True)
    shown = len(frame("get_observations", observed)) + len(frame("get_findings", found))
    # Both fit one call at the client's default estimate, with a quarter left for the rest of the turn.
    assert shown < 0.75 * Budgets().max_input_tokens_per_call * DEFAULT_BYTES_PER_TOKEN


@pytest.mark.db
async def test_run_summary_gives_graph_counts_but_never_the_key_a_scan_resumes_after(make_ctx: Any) -> None:
    ctx = make_ctx(None)
    graph = {"groups_a": 3, "groups_b": 3, "groups_equal": 2, "groups_different": 1,
             "split": 1, "merge": 0, "rearrange": 0, "oversized": 0}  # fmt: skip
    scan = {"done": True, "after": [RAW_KEY], "rows_a": 4, "rows_b": 4, "skipped_a": 0, "skipped_b": 0,
            "matched": 2, "candidates": 2, "graph": graph}  # fmt: skip
    graphed, plain = await seed(ctx, scan=scan), await seed(ctx)

    summary = await run_tool(ctx, Scope(graphed.run_id, TESTER), "get_run_summary", {})

    assert summary["graph"] == graph and RAW_KEY not in frame("get_run_summary", summary)
    assert "graph" not in await run_tool(ctx, Scope(plain.run_id, TESTER), "get_run_summary", {})


@pytest.mark.db
async def test_run_summary_gives_the_newest_ended_cycle_s_labelled_accuracy_as_stored(
    make_ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stored score, cut to its first root causes with the rest counted,
    with ``summary`` and the sign-off attesting it; None until a labelling of the cycle is labelled. No
    key, masked or raw, and no staff identity: the line says a person labelled, not who."""
    ctx = make_ctx(None)
    seeded = await seed(ctx)
    scope = Scope(seeded.run_id, TESTER)
    assert (await run_tool(ctx, scope, "get_run_summary", {}))["accuracy"] is None  # the cycle is live
    async with ctx.pool.connection() as conn:
        await repo.end_cycle(conn, seeded.run_id)
        olga, frank = [await person(conn, name, "operator") for name in ("olga", "frank")]
    before = frame("get_run_summary", await run_tool(ctx, scope, "get_run_summary", {}))
    truth = {"source": "CBS-PROD.account_master", "as_of": "2026-10-01"}
    drawn, _ = await accuracy.store_draw(ctx, seeded.run_id, 1, who=olga, size=3, per_cause=0, truth=truth)
    assert (await run_tool(ctx, scope, "get_run_summary", {}))["accuracy"] is None  # drawn, not labelled
    header, rows = parse_csv(drawn["sheet"].encode())
    filled = [row | {"label": "B_RIGHT", "basis": "SYSTEM_OF_RECORD", "notes": RAW_EMAIL} for row in rows]
    await accuracy.store_labels(ctx, drawn["labelling_id"], render_csv(header, filled).encode(), who=frank)
    async with ctx.pool.connection() as conn:
        row = await repo.get_labelling(conn, drawn["labelling_id"])
    assert row is not None and len(row["accuracy"]["discrepant"]["by_cause"]) == 2

    found = (await run_tool(ctx, scope, "get_run_summary", {}))["accuracy"]
    monkeypatch.setattr("deltasleuth.agent.tools.TOP_CAUSES", 1)
    cut = (await run_tool(ctx, scope, "get_run_summary", {}))["accuracy"]

    stored = row["accuracy"] | {"discrepant": row["accuracy"]["discrepant"] | {"by_cause_more": 0}}
    day = f"{row['labelled_at']:%Y-%m-%d}"
    assert found == {
        "accuracy": stored, "summary": accuracy.summary(row, signoff=None, named=False), "attested": None,
    }  # fmt: skip
    assert found["summary"].startswith(
        f"{row['labelling_id']} run {seeded.run_id} cycle 1: 2 of 2 discrepancies labelled {day} by a person"
        " against CBS-PROD.account_master as of 2026-10-01 (no sign-off attests it): "
    )
    assert cut["accuracy"]["discrepant"]["by_cause"] == row["accuracy"]["discrepant"]["by_cause"][:1]
    assert cut["accuracy"]["discrepant"]["by_cause_more"] == 1
    assert cut["accuracy"]["discrepant"]["labels"] == row["accuracy"]["discrepant"]["labels"]
    block = json.dumps(found)
    assert seeded.key not in block and RAW_KEY not in block and RAW_EMAIL not in block
    monkeypatch.undo()
    shown = frame("get_run_summary", await run_tool(ctx, scope, "get_run_summary", {}))
    assert json.dumps(found["summary"]) in shown and RAW_KEY not in shown and RAW_EMAIL not in shown
    # No actor's name or id reaches the model: before the labelling none did, and it adds none.
    named = ("frank", "olga", frank.actor, olga.actor, frank.actor.removeprefix("user:"))
    assert "frank" in accuracy.summary(row, signoff=None) and (row["labelled_by"], row["drawn_by"]) == (
        frank.actor,
        olga.actor,
    )
    assert not [name for name in named if name in shown or name in before]


def test_the_accuracy_block_is_cut_to_the_rules_with_most_labelled_keys_and_counts_what_it_leaves_out() -> (
    None
):
    """Twelve rules and the mask domain, of which only the last by name has labelled keys: the cut keeps
    it, where the first ten by name held none, and says how many causes and rules it left out."""
    rules = [{"rule_id": "mask", "keys": 9, "labelled": 0}]
    rules += [{"rule_id": f"r{n:02d}", "keys": n, "labelled": 4 * (n == 11)} for n in range(12)]
    causes = [{"root_cause_id": f"RC-{n:012x}", "rank": n + 1} for n in range(25)]
    cut = _top({"discrepant": {"by_cause": causes}, "accepted": {"by_rule": rules}, "agreed": None})
    assert cut["discrepant"] == {"by_cause": causes[:10], "by_cause_more": 15}
    assert [rule["rule_id"] for rule in cut["accepted"]["by_rule"]] == [
        "r11", "r10", "mask", "r09", "r08", "r07", "r06", "r05", "r04", "r03",
    ]  # fmt: skip
    assert cut["accepted"]["by_rule_more"] == 3 and cut["agreed"] is None
    assert _top({"discrepant": None, "accepted": None, "agreed": None}) == dict.fromkeys(
        ("discrepant", "accepted", "agreed")
    )
    (summary,) = [tool for tool in TOOLS if tool.name == "get_run_summary"]
    assert "first 10 root causes and the 10 rules with most labelled keys" in summary.description
    assert "by_cause_more and by_rule_more count the ones left out" in summary.description


@pytest.mark.db
async def test_a_key_the_run_lacks_is_named_with_its_run_and_frames_hold_no_time(make_ctx: Any) -> None:
    """One text for the chat and for MCP; and ``jsonio.plain``, which MCP applies
    before it frames a result, leaves every result of the chat's tools as it is."""
    ctx = make_ctx(None)
    seeded = await seed(ctx)
    scope = Scope(seeded.run_id, TESTER)
    for name in ("get_observations", "compare_key"):
        with pytest.raises(ToolError, match=f"^no key tok:x in run {seeded.run_id}$"):
            await run_tool(ctx, scope, name, {"key": "tok:x"})
    for name, args in (
        ("get_run_summary", {}),
        ("get_findings", {}),
        ("get_observations", {"key": seeded.key}),
    ):
        data = await run_tool(ctx, scope, name, args)
        assert frame(name, jsonio.plain(data)) == frame(name, data)
    at = datetime(2026, 10, 5, 2, 5, tzinfo=UTC)
    stored = {"at": at, "days": [at.date()], "addr": ipaddress.ip_address("10.1.2.3"), "n": 1}
    assert jsonio.plain(stored) == {
        "at": "2026-10-05T02:05:00+00:00", "days": ["2026-10-05"], "addr": "10.1.2.3", "n": 1,
    }  # fmt: skip


def test_the_system_prompt_is_its_four_paragraphs_unchanged() -> None:
    """The MCP server's instructions say three of them; the chat's prompt is as it was."""
    assert prompt.SYSTEM == "\n\n".join((prompt.INTRO, prompt.UNTRUSTED, prompt.MASKED, prompt.CITE))
    assert hashlib.sha256(prompt.SYSTEM.encode()).hexdigest() == (
        "9513f64641dbddb9c0c2786f5b61aba4e54e2b91be96051e35645ca2dbf8855d"
    )
    assert prompt.SYSTEM.startswith("You answer questions about DeltaSleuth comparison runs. A run reads the")
    assert prompt.SYSTEM.endswith(
        "If the tools cannot answer the question, say so, citing what you looked up."
    )
    assert len(prompt.SYSTEM) == 965


def test_the_fixed_questions_hold_no_digit() -> None:
    """The validator would refuse a number the model echoed from its question. Both say the same rules;
    only EXPLAIN names an id, the root cause's."""
    for text in (prompt.EXPLAIN, prompt.SUMMARISE):
        assert not re.search(r"\d", text) and prompt._RULES in text
    assert prompt.EXPLAIN.count("{id}") == 2 and "{" not in prompt.SUMMARISE
    asked = prompt.EXPLAIN.format(id="RC-4361f6bb1772")
    assert asked.startswith("Explain root cause RC-4361f6bb1772 of this run to a reader who has not seen it.")
    assert asked.endswith(" Cite [RC-4361f6bb1772] and every finding, observation and key you rely on.")


@pytest.mark.db
async def test_get_findings_applies_its_filters(make_ctx: Any) -> None:
    ctx = make_ctx(None)
    seeded = await seed(ctx)
    scope = Scope(seeded.run_id, TESTER)
    every = (await run_tool(ctx, scope, "get_findings", {}))["findings"]  # email and total differ
    total = next(f for f in every if f["path_pattern"] == "$.total")
    for name in ("root_cause_id", "taxonomy_code", "path_pattern", "severity"):
        found = (await run_tool(ctx, scope, "get_findings", {name: total[name]}))["findings"]
        assert total in found and all(f[name] == total[name] for f in found), name


@pytest.mark.db
async def test_tools_see_only_the_chats_run_and_its_spec(
    make_ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_ctx(None)
    seeded, other = await seed(ctx), await seed(ctx)
    compared: list[int] = []

    async def compare_one(ctx: Any, spec_id: int, key: str, *, who: Any) -> Any:
        compared.append(spec_id)
        assert who == TESTER  # the asker
        raise LookupError("stop here")

    monkeypatch.setattr("deltasleuth.runner.compare_one", compare_one)
    scope = Scope(seeded.run_id, TESTER)

    refused = ("get_findings", {"run_id": other.run_id}), ("compare_key", {"spec_id": 1, "key": "x"})
    for name, args in refused:
        with pytest.raises(ToolError, match="invalid arguments"):  # no tool takes a run or a spec
            await run_tool(ctx, scope, name, args)
    with pytest.raises(LookupError):
        await run_tool(ctx, scope, "compare_key", {"key": seeded.key})

    assert compared == [seeded.spec_id]  # the run's own spec: another could mask less
    found = (await run_tool(ctx, scope, "get_findings", {}))["findings"]
    assert {f["finding_id"] for f in found} == set(seeded.findings.values())  # not the other run's


@pytest.mark.db
async def test_a_spec_version_lacking_its_approval_is_a_tool_error_the_model_reads(
    make_ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_ctx(None)
    seeded = await seed(ctx)
    needs = f"spec {seeded.spec_id} (agent-demo 1.0.0) needs a four-eyes approval of clear before it runs"

    async def compare_one(*_: Any, **__: Any) -> Any:
        raise ApprovalRequired(needs)  # as under SSO, until a second person approves the version

    monkeypatch.setattr("deltasleuth.runner.compare_one", compare_one)
    with pytest.raises(ToolError, match=f"^{re.escape(needs)}$"):
        await run_tool(ctx, Scope(seeded.run_id, TESTER), "compare_key", {"key": seeded.key})


@pytest.mark.db
async def test_a_refused_connect_tells_the_model_the_side_and_the_target_only(
    make_ctx: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The plan words a refused connect for whoever creates the run, with the endpoint its target declares.
    # That text once reached the model's host and the stored transcript as it was.
    (tmp_path / "password").write_text("s3cret")
    database = DatabaseTarget(
        "oracle", "127.0.0.1", 9, "CRMPDB", "ds_reader", f"file:{tmp_path}/password", allow_unencrypted=True
    )
    crm = Target("db-a", "", allow_private_network=True, database=database)
    registry = TargetRegistry({"db-a": crm, "svc": replace(TARGETS.get("svc-a"), target_id="svc")})
    ctx = make_ctx(None)
    seeded = await seed(ctx)

    async def compare_one(*_: Any, **__: Any) -> Any:  # as far as its plan, which finds no listener
        await plan(registry, compiled({"name": "customers", "keyColumns": ["id"]}))

    monkeypatch.setattr("deltasleuth.runner.compare_one", compare_one)
    with pytest.raises(ValueError, match="cannot connect") as created:  # what a run's creator reads
        await compare_one()
    assert str(created.value) == (
        "side a: cannot connect to target db-a (oracle 127.0.0.1:9/CRMPDB as ds_reader):"
        " ConnectionRefusedError: no server answered"
    )
    with pytest.raises(ToolError) as told:
        await run_tool(ctx, Scope(seeded.run_id, TESTER), "compare_key", {"key": seeded.key})
    assert str(told.value) == "side a: cannot connect to target db-a"


@pytest.mark.parametrize(
    ("name", "args", "problem"),
    [
        (
            "get_findings",
            {"limit": 51},
            "invalid arguments: 51 is greater than the maximum of 50",
        ),
        ("get_findings", None, "invalid arguments: None is not of type 'object'"),
        ("drop_tables", {}, "unknown tool 'drop_tables'"),
    ],
)
async def test_bad_calls_are_refused_before_any_read(name: str, args: Any, problem: str) -> None:
    with pytest.raises(ToolError, match=f"^{problem}$"):
        await run_tool(None, Scope("R-1", TESTER), name, args)  # type: ignore[arg-type]


VIEWER = replace(TESTER, roles=expand(["viewer"]))


def test_the_tools_offered_follow_the_askers_roles() -> None:
    assert [t.name for t in tools_for(VIEWER)] == ["get_run_summary", "get_findings", "get_observations"]
    assert tools_for(replace(TESTER, roles=expand(["operator"]))) == tools_for(TESTER) == TOOLS
    assert tools_for(replace(TESTER, roles=frozenset())) == ()


async def test_a_tool_the_asker_may_not_use_is_refused() -> None:
    with pytest.raises(ToolError, match=r"^compare_key needs the operator role$"):
        await run_tool(None, Scope("R-1", VIEWER), "compare_key", {"key": "K"})  # type: ignore[arg-type]
