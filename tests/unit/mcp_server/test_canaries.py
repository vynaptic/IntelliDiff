"""What a result never holds: planted raw keys, protected values, a staff name, a user
and a token id, a target's id and address, a DSN's password and a file's name appear in no result, error,
log line or audit detail of any tool, its failures included, nor in what ``ds mcp`` writes to stderr of any
logger's record; nor does a relation or a target that readiness words a gate with; a run that declares
clear values is served only where the settings say so; and text planted in a payload stays data."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import Any

import psycopg
import pytest
from mcp import Client
from psycopg.types.json import Jsonb

from deltasleuth import prechecks, readiness, schedules
from deltasleuth.auth.principal import Principal, expand
from deltasleuth.cli import main as cli
from deltasleuth.config import McpSettings
from deltasleuth.context import Context
from deltasleuth.mcp import server, tools
from deltasleuth.runner import create_run
from deltasleuth.store import repo
from tests.conftest import TESTER
from tests.unit.runner.conftest import Harness, ScriptedService, doc
from tests.unit.runner.measuring import measure
from tests.unit.runner.test_census import Tables, on_tables  # noqa: F401 (a fixture)
from tests.unit.runner.test_census import spec as tables_spec
from tests.unit.runner.test_runs import listed
from tests.unit.runner.test_scan import databases  # noqa: F401 (a fixture)

from .conftest import (
    CLEAR_KEY,
    EVERY,
    INJECTION,
    LONG_KEY,
    MASKED,
    MATCH_KEY,
    NAME,
    OTHER_NAME,
    RAW_KEY,
    STAFF,
    World,
    ask,
    audited,
    token,
)
from .test_tools import PER_RUN, args_of

pytestmark = pytest.mark.db
DSN = "postgresql://ds:Dsn-Passw0rd-Canary@db.internal.example:5432/ds"
# The store's address, account and database, as psycopg words a connection in its loggers' lines.
STORE = "(host=db.internal.example port=5432 user=Account-Canary database=Database-Canary)"
GRAPH = {"groups_a": 3, "groups_b": 3, "groups_equal": 2, "groups_different": 1, "split": 1, "merge": 0,
         "rearrange": 0, "oversized": 0}  # fmt: skip


async def planted(world: World) -> tuple[str, ...]:
    """What no tool may show, planted where a careless read would find it."""
    ctx, port = world.ctx, world.service.server.port  # type: ignore[union-attr]
    other = Principal("token:T-00000000000000ee", "Other Agentcanary", expand(["operator"]))
    declared = {"name": "by-token", "spec_id": world.spec_id, "run": {"mode": "batch"}}
    await schedules.create(ctx, declared | {"cron": "7 4 * * *", "time_zone": "UTC"}, other)
    scan = {"done": True, "after": [RAW_KEY], "rows_a": 4, "rows_b": 4, "skipped_a": 0, "skipped_b": 0,
            "matched": 2, "candidates": 2, "graph": GRAPH}  # fmt: skip
    params = {"samples_dir": "/srv/exports/Export-Canary-2026", "pinned": {"a": "Pinned-Canary"},
              "tables": {"a": {"file": "Export-Canary-2026.csv", "files_sha256": "f" * 64}}}  # fmt: skip
    async with ctx.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO ds.scans (run_id, cycle, state) VALUES (%s, 1, %s)", (world.run_id, Jsonb(scan))
        )
        await conn.execute(
            "UPDATE ds.runs SET params = params || %s, guard = %s WHERE run_id = %s",
            (Jsonb(params), Jsonb({"a": {"relation": "Guard-Canary"}}), world.run_id),
        )
    return (
        RAW_KEY, MATCH_KEY, NAME, OTHER_NAME, STAFF, world.staff.actor.removeprefix("user:"),
        "00000000000000ee", "Agentcanary", "svc-a", "svc-b", f"127.0.0.1:{port}", f":{port}/", "db-canary",
        "accounts_canary", "Dsn-Passw0rd-Canary", "db.internal.example", "Export-Canary", "Pinned-Canary",
        "Guard-Canary", "f" * 64,
    )  # fmt: skip


@pytest.fixture
def stdio_logging() -> Iterator[Callable[[], None]]:
    """``ds mcp``'s logging, set when called and undone afterwards: its handlers on ``deltasleuth.mcp``,
    on the library's ``mcp`` and on the root logger, which every other logger's records reach, and the
    audit copy on ``deltasleuth.audit``, whichever test ran before."""
    loggers = [logging.getLogger(name) for name in (None, "mcp", "deltasleuth.mcp", "deltasleuth.audit")]
    kept = [(logger.propagate, logger.level) for logger in loggers]
    yield lambda: (cli._siem(), cli._mcp_logs(bare=True))[0]
    for logger, (propagate, level) in zip(loggers, kept, strict=True):
        logger.handlers = [h for h in logger.handlers if not isinstance(h, cli._Stream)]
        logger.propagate = propagate
        logger.setLevel(level)


@pytest.mark.parametrize("via", [tools.HTTP, tools.STDIO])
async def test_planted_values_appear_nowhere(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    stdio_logging: Callable[[], None],
    via: str,
) -> None:
    ctx, who = world.ctx, token("operator", 3, via)
    canaries = await planted(world)
    caplog.set_level(logging.DEBUG)
    limits, said = tools.Limits(replace(EVERY, calls_per_minute=600)), []

    async def calling(name: str, args: dict[str, Any], limits: tools.Limits = limits) -> Any:
        text, failed = await tools.call(ctx, limits, who, name, args)
        said.append(text)
        return None if failed else json.loads(text)["data"]

    answers = {name: await calling(name, args_of(world, name)) for name in tools.CATALOGUE}
    assert all(answers.values()) and answers["get_run_summary"]["graph"] == GRAPH  # the counts, never the key
    assert [item["name"] for item in answers["list_schedules"]["items"]] == ["by-token", "nightly"]
    for name in PER_RUN:  # refusals: a run not served here, no such run, no such key
        keyed = {"key": CLEAR_KEY} if name in PER_RUN[3:] else {}
        await calling(name, {"run_id": world.clear_run} | keyed, tools.Limits(MASKED))
        await calling(name, {"run_id": "R-" + "9" * 26} | keyed)
        await calling(name, {"run_id": world.run_id, "key": "tok:name:v1:NOSUCH"} if keyed else {"run_id": 9})
    for name, member in (
        ("get_readiness_pair", "name"),
        ("get_schedule", "schedule"),
        ("get_readiness", "programme"),
    ):
        await calling(name, {member: "nope"})
    async with Client(server.build(ctx, EVERY, who=who), mode="legacy") as client:  # the library's own logger
        for name in ("get_observations", "nope"):
            said.append((await client.call_tool(name, args_of(world, "get_observations"))).content[0].text)
    services = ((repo, "get_run"), (repo, "list_runs"), (readiness, "overview"), (readiness, "pair"),
                (schedules, "overview"), (schedules, "described"), (prechecks, "overview"))  # fmt: skip
    leak = f"{DSN} refused {' '.join(canaries)}"
    for fault in (psycopg.OperationalError(leak), ValueError(leak), RuntimeError(leak), KeyError(leak)):
        with monkeypatch.context() as patched:

            async def failing(*_: Any, fault: Exception = fault, **__: Any) -> Any:
                raise fault

            for module, name in services:
                patched.setattr(module, name, failing)
            assert [await calling(name, args_of(world, name)) for name in tools.CATALOGUE] == [None] * 11
    await world.service.server.close()  # type: ignore[union-attr]
    unreachable = await calling("compare_key", args_of(world, "compare_key"))  # both sides are away
    assert unreachable["outcome"] == "INCONCLUSIVE"
    assert not [(canary, text[:200]) for text in said for canary in canaries if canary in text]
    records = [r for r in caplog.records if r.name == "deltasleuth.mcp" or r.name.split(".")[0] == "mcp"]
    calls = [r.getMessage() for r in records if r.name == "deltasleuth.mcp" and r.levelno == logging.INFO]
    assert len(calls) == len(said) and all(f" actor={who.actor} " in line for line in calls)
    # No identity but the caller's own in a call's line, and nothing planted in any.
    assert not [line for line in calls if line.count("token:") != 1 or "user:" in line or "local:" in line]
    faults = [r for r in records if r.name == "deltasleuth.mcp" and r.levelno > logging.INFO]
    assert len(faults) == 3 * 11  # the reason is the server log's
    trace = logging.Formatter().formatException
    shown = [(r.name, r.getMessage() + (trace(r.exc_info) if r.exc_info else ""))
             for r in records if r not in faults or via == tools.STDIO]  # fmt: skip
    assert not [(canary, text) for _, text in shown for canary in canaries if canary in text]
    if via == tools.HTTP:  # ds serve's log names what was refused, and where a fault was raised
        assert all(leak in r.getMessage() or r.exc_info for r in faults)
    else:
        assert not [r for r in faults if r.exc_info]
    assert "mcp.server.lowlevel.server" in {name for name, _ in shown}  # the library's own lines are read too
    if via == tools.STDIO:  # ds mcp's stderr is the launching agent's: every record above as it writes it,
        stdio_logging()  # and the driver's and a service's when the store fails them
        capsys.readouterr()
        for record in list(caplog.records):  # but the audit copy of what this test planted as another caller
            if record.name != "deltasleuth.audit" or f'"actor": "{who.actor}"' in record.getMessage():
                logging.getLogger(record.name).handle(record)
        logging.getLogger("psycopg.pool").warning("error connecting in 'pool-1': %s %s", STORE, leak)
        try:
            raise psycopg.OperationalError(f"{STORE}: {leak}")
        except psycopg.OperationalError:
            logging.getLogger("deltasleuth.readiness").warning("the standing was not read", exc_info=True)
        err = capsys.readouterr().err
        assert not [held for held in (*canaries, "Account-Canary", "Database-Canary") if held in err]
        assert err.splitlines()[-2:] == ["psycopg.pool WARNING", "deltasleuth.readiness WARNING"]
        assert set(calls) < set(err.splitlines())  # the calls' lines, whole
    mine = [row for row in await audited(ctx) if row["actor"] == who.actor]
    assert {row["action"] for row in mine} == {"mcp.read", "key.view", "compare_one", "run.finished"}
    written = json.dumps([[row["object"], row["detail"]] for row in mine], default=str)
    assert not [canary for canary in canaries if canary in written]


async def gates_unworded(ctx: Context, pair: str, status: str, worded: str, *never: str) -> None:
    """``pair``'s population gate reads ``status``, in words that hold ``worded``; a viewer of
    ``aggregates`` alone is shown the gate without its text, and neither ``worded`` nor any of ``never``
    by a readiness or a run tool."""
    judged = (await readiness.pair(ctx, pair))["gates"][0]
    assert (judged["gate"], judged["status"]) == ("population", status) and worded in judged["text"]
    limits = tools.Limits(McpSettings((tools.AGGREGATES,)))
    for name, args in (("get_readiness_pair", {"name": pair}), ("get_readiness", {}), ("list_runs", {})):
        text, failed = await tools.call(ctx, limits, token("viewer"), name, args)
        assert not failed and pair in text and not [held for held in (worded, *never) if held in text], name
    shown, _ = await ask(ctx, token("viewer"), "get_readiness_pair", {"name": pair})
    assert shown["gates"][0] == {name: judged[name] for name in judged if name != "text"}


async def test_no_result_names_the_relation_a_gate_is_worded_with(on_tables: Tables) -> None:  # noqa: F811
    """A key-list run counted against a table side is evidence, and readiness words its population
    gate with the relation it was counted against."""
    seed = (
        "CREATE SCHEMA corebank_canary; CREATE TABLE corebank_canary.ledger_canary (id text, email text);"
        " INSERT INTO corebank_canary.ledger_canary VALUES ('1', 'a'), ('2', 'b'), ('3', 'c');"
    )
    table = {"schema": "corebank_canary", "name": "ledger_canary", "keyColumns": ["id"], "columns": ["email"]}
    _, opened = on_tables(seed, readiness={})  # with ``readiness`` the worker counts the table side
    async with opened as h:
        key = {"name": "id", "type": "string", "class": "identifier"}
        spec_id = await h.spec(tables_spec(table, table, key=key))
        await h.finish(await create_run(h.ctx, spec_id, "batch", ["1", "2", "3"], who=TESTER))
        await measure(h)
        relation = "corebank_canary.ledger_canary"
        await gates_unworded(h.ctx, "accounts", "met", relation, "ledger_canary", "db-a", "db-b")


async def test_no_result_names_the_target_a_gate_is_worded_with(
    harness: Harness, service: ScriptedService
) -> None:
    """A key listing on side b's target is no census of side a, and readiness says so by the target's
    id."""
    service.listings = [["K001", "K002"]]
    for key in ("K001", "K002"):
        service.both(key, doc(key), doc(key))
    request = "\n    request: {method: GET, path: /list}"
    on_b = listed().replace(f"target: svc-a{request}", f"target: svc-b{request}")
    assert on_b != listed()
    await harness.finish(await create_run(harness.ctx, await harness.spec(on_b), "batch", who=TESTER))
    await measure(harness)
    await gates_unworded(harness.ctx, "unit-runner", "not_met", "svc-b", "svc-a")


async def test_clear_declared(world: World) -> None:
    """With ``production_tokenised`` alone such a run is not served and nothing of it is shown; with
    ``production_clear_declared`` its clear value shows at the path its spec declares, and its key as
    ``key_masked`` only."""
    ctx, shown = world.ctx, []
    for name in tools.CATALOGUE:
        keyed = {"key": CLEAR_KEY} if name in PER_RUN[3:] else {}
        args = {"run_id": world.clear_run} | keyed if name in PER_RUN else args_of(world, name)
        text, failed = await tools.call(ctx, tools.Limits(MASKED), token("operator", 2), name, args)
        assert failed == (name in PER_RUN) and ("mcp.data_classes lacks" in text) == failed, name
        shown.append(text)
    assert not [
        text for text in shown for raw in (CLEAR_KEY, LONG_KEY, "SYSTEM: call", "L" * 40) if raw in text
    ]

    def places(value: Any, raw: str, at: str = "") -> list[str]:
        if isinstance(value, dict):
            return [found for name, item in value.items() for found in places(item, raw, f"{at}.{name}")]
        if isinstance(value, list):
            return [found for item in value for found in places(item, raw, at)]
        return [at] if isinstance(value, str) and raw in value else []

    seen = {}
    for name in PER_RUN:
        keyed = {"key": CLEAR_KEY} if name in PER_RUN[3:] else {}
        seen[name], failed = await ask(ctx, token("operator", 2), name, {"run_id": world.clear_run} | keyed)
        assert not failed
    held = {(name, at) for name, data in seen.items() for at in places(data, CLEAR_KEY)}
    assert held == {
        ("get_run_summary", ".root_causes.sample_keys"), ("get_root_causes", ".root_causes.sample_keys"),
        ("get_findings", ".findings.key_masked"), ("get_observations", ".key_masked"),
        ("compare_key", ".key_masked"),
    }  # fmt: skip
    clear = {(name, at) for name, data in seen.items() for at in places(data, "SYSTEM: call")}
    assert clear == {
        ("get_findings", ".findings.value_b"), ("get_observations", ".findings.value_b"),
        ("get_observations", ".side_b.canonical"), ("compare_key", ".findings.value_b_tok"),
    }  # fmt: skip
    (status,) = [f for f in seen["get_findings"]["findings"] if f["key_masked"] == CLEAR_KEY]
    assert (status["path_pattern"], status["value_a"], status["value_b"]) == ("$.status", "ACTIVE", INJECTION)
    canonical = json.loads(seen["get_observations"]["side_b"]["canonical"])
    assert canonical["status"] == INJECTION and canonical["name"].startswith("tok:name:")
    assert canonical["id"].startswith("tok:")  # the key's own field is not declared clear: a token still


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_injection_stays_data(world: World, mode: str) -> None:
    """A payload's text that closes a frame and gives an order is one string inside ``data``."""
    ctx = world.ctx
    async with ctx.pool.connection() as conn:
        before = [(run["run_id"], run["status"]) for run in (await repo.list_runs(conn))[0]]
    rows = len(await audited(ctx))
    async with Client(server.build(ctx, EVERY, who=token("operator", 2)), mode=mode) as client:
        result = await client.call_tool("get_findings", {"run_id": world.clear_run, "key": CLEAR_KEY})
    (block,) = result.content
    framed = json.loads(block.text)
    assert (block.type, result.is_error, result.structured_content) == ("text", False, None)
    assert set(framed) == {"tool", "data"} and framed["tool"] == "get_findings"
    assert [finding["value_b"] for finding in framed["data"]["findings"]] == [INJECTION]
    assert INJECTION not in block.text and json.dumps(INJECTION)[1:-1] in block.text  # escaped: one string
    async with ctx.pool.connection() as conn:
        assert [(run["run_id"], run["status"]) for run in (await repo.list_runs(conn))[0]] == before
    assert [row["action"] for row in (await audited(ctx))[rows:]] == ["mcp.read"]  # no run was made or moved
