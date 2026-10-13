"""Runs with a file side: a mixed pair reads its file side a pass a group
and its service side key by key; a recheck reads no side of fixed files again, whose first observation stands,
and none is due where their bytes alone would answer it; two file sides are hashed in DuckDB and their per-key
results are final as read; a changed file, rows the plan did not count and an error's class fail the run in
fixed words; a read error of unchanged bytes fails it at the third pass, checkpoint or not; a mount that does
not answer costs its own runs only; per-file sizes, times and digests are in no answer, audit row or plan. A
reader's process that ends pauses a pass, and at the third end charged to a
side's files fails their run as ReaderDied, the worker and the other runs going on."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import pytest

from deltasleuth import approvals
from deltasleuth.api.deps import shown
from deltasleuth.connectors import tables
from deltasleuth.connectors.tables import (
    FilesChanged,
    TargetDenied,
    TargetRefused,
    TargetTempSpace,
    TargetUnreachable,
    duck,
    manifest,
)
from deltasleuth.connectors.tables.mysql import MySQL
from deltasleuth.core import notify
from deltasleuth.core.types import Canonical, Confirmation, KeyResult, Outcome, Side, SideState
from deltasleuth.runner import census, run_worker
from deltasleuth.runner import scan as scanning
from deltasleuth.runner.compare import LiveReader
from deltasleuth.runner.runs import compare_one, create_run
from deltasleuth.security.crypto import Sealer
from deltasleuth.store import repo
from tests.conftest import TESTER, MySQLServer
from tests.support import faults

from .conftest import Harness, OpenHarness, ScriptedService, doc, spec_text

pytestmark = pytest.mark.db

Opened = Callable[..., AbstractAsyncContextManager[Harness]]
HEADER = "id|name|status\n"


def rows(*numbers: int, status: str = "A") -> str:
    return "".join(f"K{n:03d}|n{n}|{status}\n" for n in numbers)


def table(path: str = "export.csv", **more: Any) -> dict[str, Any]:
    dialect = {"columns": ["id", "name", "status"], "delimiter": "|", "header": True}
    found = {
        "name": "customers",
        "keyColumns": ["id"],
        "file": {"path": path, "format": "csv", "csv": dialect},
    }
    return found | more


def spec(
    a: dict[str, Any], b: dict[str, Any], keys: list[str] | None = None, name: str = "exports", **members: Any
) -> str:
    """Side a (a table of target ``fa``) against side b: a table of ``fb``, or the scripted service."""
    key = {"name": "id", "type": "string", "class": "business_code", "pattern": "^K[0-9]{3}$"}
    if keys is not None:
        key["source"] = {"kind": "list", "keys": keys}
    return json.dumps(
        {
            "apiVersion": "deltasleuth/v1",
            "kind": "ComparisonSpec",
            "metadata": {"name": name, "version": "1.0.0", "owner": "tests"},
            "normalizerVersion": "1.1.0",
            "key": key,
            "sides": {"a": {"role": "legacy", "target": "fa", "table": a}, "b": b},
            "compare": {},
            "recheck": {"delay": "0s", "times": 1, "oneShot": {"delay": "0s", "times": 1}},
            "security": {"unlisted": "clear"},
            "execution": {"concurrency": 1, "retries": 0},
        }
        | members
    )


SERVICE = {
    "role": "new", "target": "svc-b", "request": {"method": "GET", "path": "/b/{{ key }}"},
    "notFound": {"status": [404]}, "truncation": {"itemsPath": "$.tags", "cap": 50},
}  # fmt: skip


def other(b: dict[str, Any]) -> dict[str, Any]:
    return {"role": "new", "target": "fb", "table": b}


def served(n: int, status: str = "A") -> dict[str, Any]:
    return {"name": f"n{n}", "status": status}


@pytest.fixture
def exports(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """``files.roots_under``, outside the harness's own directories, as its real path."""
    found = tmp_path_factory.mktemp("exports").resolve()
    (found / "a").mkdir()
    (found / "b").mkdir()
    return found


@pytest.fixture
def opened(open_harness: OpenHarness, service: ScriptedService, exports: Path) -> Opened:
    """``async with opened(**worker) as h``: targets ``fa`` and ``fb`` read ``exports / 'a'`` and ``/ 'b'``,
    ``svc-a`` and ``svc-b`` are the scripted service; one slot, so a group is what one lease takes."""

    @asynccontextmanager
    async def open_(**worker: Any) -> AsyncIterator[Harness]:
        assert service.server is not None
        root = {"engine": "files", "settle_s": 0, "allow_writable": True, "rate_per_s": 1000}
        found = {
            "fa": root | {"root": str(exports / "a")},
            "fb": root | {"root": str(exports / "b")},
            "svc-b": {
                "base_url": f"http://127.0.0.1:{service.server.port}", "allow_private_network": True,
                "allow_insecure_http": True, "rate_per_s": 1000,
                "capabilities": {"authoritative_absence": {"status": [404]}},
            },
        }  # fmt: skip
        found["svc-a"] = found["svc-b"]
        files = {"roots_under": str(exports), "memory": "256MB", "spill": "1GB"}
        settings = {"concurrency": 1, "lease_seconds": 1} | worker
        async with open_harness(json.dumps({"targets": found}), files=files, **settings) as h:
            yield h

    return open_


@pytest.fixture
def statements(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every statement a cursor of the file reader is sent, in order."""
    sent: list[str] = []
    real = duck.Cursor.execute

    async def execute(self: duck.Cursor, statement: str, *args: Any, **kwargs: Any) -> None:
        sent.append(statement)
        await real(self, statement, *args, **kwargs)

    monkeypatch.setattr(duck.Cursor, "execute", execute)
    return sent


def per_key(sent: list[str]) -> int:
    """The per-key statements among ``sent``: each one pass over a side's files."""
    return sum("QUALIFY" in statement for statement in sent)


# --- mixed pairs read files in groups; rechecks never read a fixed side ---------------------------


async def test_mixed_pair_reads_the_file_side_in_groups(
    opened: Opened, service: ScriptedService, exports: Path, statements: list[str]
) -> None:
    """250 keys of a CSV side against a service are two passes over the files (200 keys and 50), not 250;
    the service reads each key by itself, and again where a recheck is due: the file side's first observation
    stands, so the recheck names the same observation and sends the files no statement."""
    numbers = list(range(1, 251))
    (exports / "a" / "export.csv").write_text(HEADER + rows(*numbers))
    for n in numbers:
        service.script("b", f"K{n:03d}", (200, served(n, "B" if n in (7, 207) else "A")))
    service.script("b", "K009", (200, served(9, "B")), (200, served(9)))  # it differs, then converges
    keys = [f"K{n:03d}" for n in numbers]
    async with opened() as h:
        run_id = await create_run(h.ctx, await h.spec(spec(table(), SERVICE, keys)), "batch", who=TESTER)
        statements.clear()  # the plan's
        run = await h.finish(run_id)
        found = await h.results(run_id)
        observed = await h.observations(run_id, "K009")
    assert run["status"] == "completed" and per_key(statements) == 2
    rechecked = {"K007", "K009", "K207"}
    assert {key for (_, key), calls in service.calls.items() if calls == 2} == rechecked
    assert sum(service.calls.values()) == 253
    assert {key: (r["outcome"], r["confirmation"]) for key, r in found.items() if key in rechecked} == {
        "K007": ("MISMATCH", "rechecked"),
        "K009": ("DRIFT", "rechecked"),
        "K207": ("MISMATCH", "rechecked"),
    }
    assert {r["outcome"] for key, r in found.items() if key not in rechecked} == {"MATCH"}
    # One observation of the file side, which the recheck's result names beside the service's second.
    assert [(o["side"], o["pass_no"]) for o in observed] == [("a", 1), ("b", 1), ("b", 2)]
    assert (found["K009"]["obs_a_id"], found["K009"]["obs_b_id"]) == (
        observed[0]["obs_id"],
        observed[2]["obs_id"],
    )


async def test_no_recheck_is_due_where_fixed_bytes_alone_would_answer(
    opened: Opened, service: ScriptedService, exports: Path, statements: list[str]
) -> None:
    """A key on no row of a side with a line that was not a row, and a key several rows hold,
    are inconclusive as read: those bytes cannot answer differently, so the service is not read again."""
    (exports / "a" / "export.csv").write_text(HEADER + rows(1, 2, 2) + "short\n")
    for n in (1, 2, 3):
        service.script("b", f"K{n:03d}", (200, served(n)))
    async with opened() as h:
        doc = spec(table(), SERVICE, ["K001", "K002", "K003", "K004"])
        run_id = await create_run(h.ctx, await h.spec(doc), "batch", who=TESTER)
        statements.clear()
        run = await h.finish(run_id)
        found = await h.results(run_id)
    assert run["status"] == "completed" and per_key(statements) == 1
    assert {key: (r["outcome"], r["inconclusive_reason"], r["confirmation"]) for key, r in found.items()} == {
        "K001": ("MATCH", None, "provisional"),
        "K002": ("INCONCLUSIVE", "identity_ambiguous", "provisional"),
        "K003": ("INCONCLUSIVE", "unparseable", "provisional"),  # never NOT_FOUND_A
        "K004": ("INCONCLUSIVE", "unparseable", "provisional"),
    }
    assert found["K003"]["attrs"]["side_a_detail"] == (
        "no row read holds the key, and 1 line(s) of this side's files were not rows"
    )
    assert dict(service.calls) == {("b", f"K{n:03d}"): 1 for n in (1, 2, 3, 4)}


async def test_a_recheck_reads_a_fixed_side_whose_first_read_failed(
    opened: Opened,
    service: ScriptedService,
    exports: Path,
    statements: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read that failed (temporary space met by the group's pass, read once more and met
    again) observed no bytes, so nothing of it stands: the recheck reads the side, in one statement."""
    (exports / "a" / "export.csv").write_text(HEADER + rows(1, 2))
    service.script("b", "K001", (200, served(1)))
    service.script("b", "K002", (200, served(2, "B")))
    real, failures = duck.Files.rows, [TargetTempSpace("OutOfMemoryException")] * 2

    async def rows_(self: duck.Files, *args: Any) -> list[Any]:
        if failures:
            raise failures.pop()
        return await real(self, *args)

    async with opened() as h:
        run_id = await create_run(
            h.ctx, await h.spec(spec(table(), SERVICE, ["K001", "K002"])), "batch", who=TESTER
        )
        monkeypatch.setattr(duck.Files, "rows", rows_)
        statements.clear()
        run = await h.finish(run_id)
        found = await h.results(run_id)
        observed = await h.observations(run_id, "K001")
    assert run["status"] == "completed_with_issues" and failures == [] and per_key(statements) == 1
    assert [(o["side"], o["pass_no"], o["error_class"]) for o in observed] == [
        ("a", 1, "connect_error"), ("b", 1, None), ("a", 2, None), ("b", 2, None),
    ]  # fmt: skip
    # The recheck is the first comparable read: a match is final, a discrepancy still owed its recheck.
    assert {key: (r["outcome"], r["first_outcome"], r["confirmation"]) for key, r in found.items()} == {
        "K001": ("MATCH", "INCONCLUSIVE", "rechecked"),
        "K002": ("MISMATCH", "INCONCLUSIVE", "provisional"),
    }


async def test_what_stands_of_a_fixed_side_is_stat_checked_once(
    opened: Opened, service: ScriptedService, exports: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recheck's reused observation follows one stat check of the files for its group: a
    changed file fails the run, and files that do not answer keep nothing, so the keys are read as any."""
    export = exports / "a" / "export.csv"
    export.write_text(HEADER + rows(1, 2))
    for n in (1, 2):
        service.script("b", f"K{n:03d}", (200, served(n, "B")))
    checks: list[bool] = []
    real = duck._verify

    async def verify(*args: Any, **kwargs: Any) -> None:
        checks.append(kwargs.get("full", False))
        await real(*args, **kwargs)

    async with opened() as h:
        spec_id = await h.spec(spec(table(), SERVICE, ["K001", "K002"]))
        run_id = await create_run(h.ctx, spec_id, "batch", who=TESTER)
        async with h.ctx.pool.connection() as conn:
            run = await repo.get_run(conn, run_id)
            compiled = await h.ctx.spec(conn, spec_id, live=True)
            plan = tables.unsealed(await h.ctx.sealer(conn, run_id), run["params"])
        reader = LiveReader(h.ctx, compiled, plan)
        assert reader.batches and reader.fixed == {Side.A}
        monkeypatch.setattr(duck, "_verify", verify)
        firsts = {key: _first(key) for key in ("K001", "K002")}
        held = await reader.standing(firsts)
        assert checks == [False] and held == {
            key: {Side.A: ("O-a", first.side_a)} for key, first in firsts.items()
        }
        failed = _first("K001", SideState.error("error_a"))  # a read that failed observed no bytes
        assert await reader.standing({"K001": failed}) == {"K001": {}} and checks == [False]

        async def away(*args: Any, **kwargs: Any) -> None:
            raise TargetUnreachable("OSError")

        monkeypatch.setattr(duck, "_verify", away)
        assert await reader.standing(firsts) == {"K001": {}, "K002": {}}
        time.sleep(0.01)
        export.write_text(HEADER + rows(1, 2, 3))
        monkeypatch.setattr(duck, "_verify", real)
        with pytest.raises(FilesChanged) as changed:
            await reader.standing(firsts)
        await reader.aclose()
    assert str(changed.value) == "side a: file 1 of 1 is not the file the run was created on (size)"


def _first(key: str, a: SideState | None = None) -> KeyResult:
    """A stored first result whose side a was observed (a row, unless ``a``) and whose side b's read
    failed."""
    a = a or SideState.complete(Canonical(doc={}, hash="h", raw_doc={}, raw_hash="r"))
    return KeyResult(key, Outcome.INCONCLUSIVE, a, SideState.error("error_b"), obs_a_id="O-a", obs_b_id="O-b")


async def test_a_soak_reads_the_same_pinned_files_each_cycle(
    opened: Opened, service: ScriptedService, exports: Path, statements: list[str]
) -> None:
    """A soak against a live side reads its pinned files once a cycle (a pass a group) and never
    for a recheck, and finds what the live side changed between two cycles."""
    (exports / "a" / "export.csv").write_text(HEADER + rows(1, 2))
    service.script("b", "K001", (200, served(1)), (200, served(1, "B")))  # it changes after cycle 1
    service.script("b", "K002", (200, served(2)))
    execution = {"concurrency": 1, "retries": 0, "cycleInterval": "0s", "stop": {"maxCycles": 2}}
    async with opened() as h:
        doc = spec(table(), SERVICE, ["K001", "K002"], execution=execution)
        run_id = await create_run(h.ctx, await h.spec(doc), "soak", soak={"rolling": False}, who=TESTER)
        statements.clear()
        run = await h.finish(run_id)
        first, second = await h.results(run_id, 1), await h.results(run_id, 2)
    assert (run["status"], run["cycle"]) == ("completed", 2) and per_key(statements) == 2
    assert {key: r["outcome"] for key, r in first.items()} == {"K001": "MATCH", "K002": "MATCH"}
    assert {key: (r["outcome"], r["confirmation"]) for key, r in second.items()} == {
        "K001": ("MISMATCH", "rechecked"),
        "K002": ("MATCH", "provisional"),
    }
    assert service.calls["b", "K001"] == 3 and service.calls["b", "K002"] == 2


async def test_compare_one_reads_a_fixed_side_once(
    opened: Opened, service: ScriptedService, exports: Path, statements: list[str]
) -> None:
    """A one-shot comparison's recheck reads the service again and the file side never; between two
    file sides no recheck is made at all."""
    _pair(exports)
    service.script("b", "K005", (200, served(5, "B")))
    async with opened() as h:
        mixed = await compare_one(h.ctx, await h.spec(spec(table(), SERVICE, ["K001"])), "K005", who=TESTER)
        once = per_key(statements)
        pair = spec(table(), other(table()), name="pair")
        fixed = await compare_one(h.ctx, await h.spec(pair), "K005", who=TESTER)
    assert (mixed.outcome, mixed.confirmation) == (Outcome.MISMATCH, Confirmation.RECHECKED)
    assert once == 1 and service.calls["b", "K005"] == 2
    assert (fixed.outcome, fixed.confirmation) == (Outcome.MISMATCH, Confirmation.FROZEN_CUT)
    assert per_key(statements) - once == 2  # one pass a side


async def test_a_refused_credential_ends_a_group_s_reads(
    opened: Opened, service: ScriptedService, exports: Path
) -> None:
    """A group of a mixed pair reads its service side key by key: the first refused credential ends the
    group's reads, pauses the run and hands every key back, so a stale secret is tried once, not 200 times."""
    (exports / "a" / "export.csv").write_text(HEADER + rows(*range(1, 41)))
    service.token = "another"  # noqa: S105 (the scripted service's)
    async with opened() as h:
        keys = [f"K{n:03d}" for n in range(1, 41)]
        run_id = await create_run(h.ctx, await h.spec(spec(table(), SERVICE, keys)), "batch", who=TESTER)
        run = await h.finish(run_id, ("paused",))
        async with h.ctx.pool.connection() as conn:
            states = (await repo.cycle_counts(conn, run_id))["keys"]
        found = await h.results(run_id)
    assert (run["status"], run["stop_reason"]) == ("paused", "run_paused_auth_error")
    assert sum(service.calls.values()) == 1 and found == {} and states == {"pending": 40}


@pytest.mark.mysql
@pytest.mark.parametrize("kind", ["VARCHAR(40)", "TEXT"])
async def test_a_group_beside_a_side_of_large_rows_is_one_pass_over_the_files(
    open_harness: OpenHarness,
    mysql_server: MySQLServer,
    exports: Path,
    statements: list[str],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """A file side is read a group of 200 keys a pass, also beside a side whose rows may be large (a
    MySQL TEXT column), which is then read LOB_BATCH keys at a time inside the group. The pair was leased 4
    keys a group, each group a pass over the files: 10 passes for 40 keys, 50 times the passes of a group of
    200."""
    keys = [f"K{n:03d}" for n in range(1, 41)]
    values = ", ".join(f"('{key}', 'n{n}', 'B')" for n, key in enumerate(keys))
    database = mysql_server.database(
        f"CREATE TABLE customers (id VARCHAR(10) PRIMARY KEY, name {kind}, status VARCHAR(5));"  # noqa: S608
        f" INSERT INTO customers VALUES {values};"
    )
    (exports / "a" / "export.csv").write_text(
        HEADER + "".join(f"{key}|n{n}|A\n" for n, key in enumerate(keys))
    )
    root = {"engine": "files", "settle_s": 0, "allow_writable": True, "rate_per_s": 1000}
    targets = {"fa": root | {"root": str(exports / "a")}, "my": mysql_server.target(database)}
    b = {"role": "new", "target": "my", "table": {"name": "customers", "keyColumns": ["id"]}}
    sliced: list[int] = []
    real = MySQL.rows

    async def rows(self: MySQL, session: Any, side: Any, asked: Any) -> Any:
        sliced.append(len(asked))
        return await real(self, session, side, asked)

    monkeypatch.setattr(MySQL, "rows", rows)
    files = {"roots_under": str(exports), "memory": "256MB", "spill": "1GB"}
    opened_ = open_harness(json.dumps({"targets": targets}), files=files, concurrency=1, lease_seconds=1)
    async with opened_ as h:
        run_id = await create_run(h.ctx, await h.spec(spec(table(), b, keys)), "batch", who=TESTER)
        statements.clear()  # the plan's
        run = await h.finish(run_id)
        found = await h.results(run_id)
    assert run["status"] == "completed" and per_key(statements) == 1
    assert {r["outcome"] for r in found.values()} == {"MISMATCH"} and len(found) == 40
    large = kind == "TEXT"  # each key twice: its first pass and its recheck, which reads the live side alone
    assert sliced == ([4] * 20 if large else [40, 40])


async def test_the_lines_csv_skip_leaves_unread_are_approved_stored_and_said(
    opened: Opened, exports: Path
) -> None:
    """``csv.skip`` leaves the first lines of each file out of what is compared, so a spec that
    declares it needs the four-eyes approval a filter needs, its side's plan stores how many lines that is,
    and the census wording says them. A headerless export's first two rows went unread without an approval,
    a count or a word: the census read 'to its end' the bytes of all five."""
    (exports / "a" / "export.csv").write_text(rows(1, 2, 3, 4, 5))  # no header: five rows
    (exports / "b" / "export.csv").write_text(rows(3, 4, 5))
    a, b = table(), table()
    a["file"]["csv"] |= {"header": False, "skip": 2}
    b["file"]["csv"] |= {"header": False}
    async with opened() as h:
        skipping = await h.spec(spec(a, other(b), security={"unlisted": "tokenize"}))
        whole = await h.spec(spec(b, other(b), name="whole", security={"unlisted": "tokenize"}))
        async with h.ctx.pool.connection() as conn:
            compiled = [await h.ctx.spec(conn, spec_id) for spec_id in (skipping, whole)]
        run = await h.finish(await create_run(h.ctx, skipping, "batch", who=TESTER))
    assert [approvals.needs(found) for found in compiled] == [["population_filters"], []]
    plan, scan = run["params"]["tables"], run["scan"]
    assert (plan["a"]["lines_skipped"], plan["b"]["lines_skipped"]) == (2, 0)
    assert (run["status"], plan["a"]["rows"], scan["rows_a"], scan["matched"]) == ("completed", 3, 3, 3)
    assert census.described(census.scanned("a", plan["a"], scan, compiled[0])).startswith(
        "read to its end from 1 fixed file(s), 50 bytes (2 line(s) at the files' starts skipped as the spec"
        " declares, not read), pinned "
    )
    assert "skipped as" not in census.described(census.scanned("b", plan["b"], scan, compiled[0]))


# --- two file sides: hashed in DuckDB, final as read -----------------------------------------------------


def _pair(exports: Path, b: str | None = None) -> None:
    """Side a: K001 to K010. Side b: K005 differs, K007 is missing, K011 is extra (or the rows ``b``)."""
    (exports / "a" / "export.csv").write_text(HEADER + rows(*range(1, 11)))
    other_ = HEADER + rows(1, 2, 3, 4) + rows(5, status="B") + rows(6, 8, 9, 10, 11)
    (exports / "b" / "export.csv").write_text(other_ if b is None else b)


SEEDED = {"K005": "MISMATCH", "K007": "NOT_FOUND_B", "K011": "NOT_FOUND_A"}


async def test_two_file_sides_are_hashed_in_duckdb_and_final_as_read(
    opened: Opened, exports: Path, statements: list[str]
) -> None:
    """A file-to-file pair's columns that both sides declare alike are hashed by DuckDB, so the worker
    renders none and the sort carries the hash, 32 bytes a row; the scan finds exactly the seeded
    differences. Each per-key result of two fixed sides is final as read (frozen_cut): no recheck is due,
    nothing is outstanding."""
    _pair(exports)
    async with opened() as h:
        scanned = await create_run(h.ctx, await h.spec(spec(table(), other(table()))), "batch", who=TESTER)
        keyed = spec(table(), other(table()), ["K001", *SEEDED], "keyed")
        listed = await create_run(h.ctx, await h.spec(keyed), "batch", who=TESTER)
        statements.clear()
        run, by_key = await h.finish(scanned), await h.finish(listed)
        found, read = await h.results(scanned), await h.results(listed)
    plan, scan = run["params"]["tables"], run["scan"]
    assert plan["a"]["digest"] == plan["b"]["digest"] == [] and plan["rendering"] == 1
    # The key text three times, 24 bytes for each of the four values carried, the hash, 16 a row.
    assert plan["a"]["sort_bytes"] == math.ceil(1.15 * 10 * (3 * 4 + 24 * 4 + 32 + 16))
    sorts = [statement for statement in statements if " ORDER BY encode(" in statement]
    assert len(sorts) == 2 and all(
        "unhex(sha256(concat(coalesce(sha256(t.\"name\"), '-'), coalesce(sha256(t.\"status\"), '-'))))"
        in statement
        for statement in sorts
    )
    assert (scan["rows_a"], scan["rows_b"], scan["matched"], scan["candidates"]) == (10, 10, 8, 3)
    # A scanned file side that holds no repeated key says 0: an absent count is a side that did not scan.
    assert (scan["repeated_a"], scan["repeated_b"]) == (0, 0)
    assert (run["status"], by_key["status"]) == ("completed", "completed")
    assert {key: (r["outcome"], r["confirmation"]) for key, r in found.items()} == {
        key: (outcome, "frozen_cut") for key, outcome in SEEDED.items()
    }
    assert {key: (r["outcome"], r["confirmation"]) for key, r in read.items()} == {
        key: (outcome, "frozen_cut") for key, outcome in ({"K001": "MATCH"} | SEEDED).items()
    }
    # One pass a side for the scan's three keys, one a side for the list's four: none read twice.
    assert per_key(statements) == 4
    for counters in (run["counters"], by_key["counters"]):
        assert (counters["outstanding"], counters["different"], counters["different_provisional"]) == (
            0,
            3,
            0,
        )
    assert by_key["counters"]["by_confirmation"]["frozen_cut"] == 4
    assert run["counters"]["by_confirmation"] == {
        "provisional": 0, "rechecked": 0, "frozen_cut": 3, "bulk_exhaustive": 8,
    }  # fmt: skip


async def test_a_column_the_sides_declare_apart_is_the_worker_s(opened: Opened, exports: Path) -> None:
    """A padded field against one that is not, and a typeMap's column, are worker columns; two padded
    fields are hashed in DuckDB without their padding, so trailing spaces alone are no difference."""
    (exports / "a" / "export.csv").write_text(HEADER + "K001|n1  |A\nK002|n2|A \n")
    (exports / "b" / "export.csv").write_text(HEADER + "K001|n1|A\nK002|n2 |B\n")
    padded = table()
    padded["file"]["csv"]["padded"] = True
    async with opened() as h:
        version = {"name": "exports", "owner": "tests"}  # one pair, three versions of its spec
        apart = spec(padded, other(table()), metadata=version | {"version": "1.1.0"})
        mapped = {"role": "legacy", "target": "fa", "table": padded, "typeMap": {"status": {"as": "string"}}}
        sides = {"a": mapped, "b": other(padded)}
        typed = spec(padded, other(padded), metadata=version | {"version": "1.2.0"}, sides=sides)
        digests = {}
        for name, doc in (("apart", apart), ("typed", typed), ("alike", spec(padded, other(padded)))):
            run = await h.finish(await create_run(h.ctx, await h.spec(doc), "batch", who=TESTER))
            digests[name] = run["params"]["tables"]["a"]["digest"]
            assert run["status"] == "completed", name
            if name == "alike":
                found, scan = await h.results(run["run_id"]), run["scan"]
    assert digests == {"apart": ["name", "status"], "typed": ["status"], "alike": []}
    assert (scan["matched"], scan["candidates"]) == (1, 1) and {
        key: r["outcome"] for key, r in found.items()
    } == {"K002": "MISMATCH"}


# --- what fails a run, and its text ----------------------------------------------------------------------


def _append(path: Path) -> None:
    with path.open("a") as export:
        export.write(rows(99))


def _earlier(path: Path) -> None:
    os.utime(path, (time.time() - 60, time.time() - 60))


def _replace(path: Path) -> None:
    path.with_suffix(".tmp").write_bytes(path.read_bytes())
    os.replace(path.with_suffix(".tmp"), path)


def _link(path: Path) -> None:
    kept = path.parent.parent / "kept.csv"
    path.rename(kept)
    path.symlink_to(kept)


def _add(path: Path) -> None:
    (path.parent / "more.csv").write_text(HEADER + rows(99))


@pytest.mark.parametrize(
    ("change", "said"),
    [
        (_append, "file 1 of 1 is not the file the run was created on (size)"),
        (_earlier, "file 1 of 1 is not the file the run was created on (modified)"),
        (lambda path: os.chmod(path, 0o600), "file 1 of 1 is not the file the run was created on (changed)"),
        (_replace, "file 1 of 1 is not the file the run was created on (replaced)"),
        (_link, "file 1 of 1 is not the file the run was created on (link)"),  # never followed
        (_add, "2 files match now, 1 when the run was created"),
    ],
    ids=["size", "modified", "changed", "replaced", "link", "added"],
)
async def test_files_changed_fails_the_run(
    opened: Opened, exports: Path, change: Callable[[Path], object], said: str
) -> None:
    """Any difference from the pinned files fails the run at its next check, naming the file by its
    position; the alert's error_type is the class."""
    _pair(exports)
    async with opened() as h:
        doc = spec(table("*.csv"), other(table()))
        run_id = await create_run(h.ctx, await h.spec(doc), "batch", who=TESTER)
        time.sleep(0.01)
        change(exports / "a" / "export.csv")
        run = await h.finish(run_id)
    assert (run["status"], run["error"]) == ("failed", f"FilesChanged: side a: {said}")
    assert notify.error_type(run["error"]) == "FilesChanged" and "export" not in run["error"]


async def test_a_per_key_read_fails_its_run_on_a_changed_file_or_a_refused_permission(
    opened: Opened, service: ScriptedService, exports: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per key: a changed file and a refused permission fail the run; neither is an
    outage to read again, and the service side is not asked for a key the files could not answer."""
    export = exports / "a" / "export.csv"
    export.write_text(HEADER + rows(1, 2))

    async def denied(self: duck.Files, *args: Any) -> list[Any]:
        raise TargetDenied("PermissionError")

    async with opened() as h:
        changed = await create_run(
            h.ctx, await h.spec(spec(table(), SERVICE, ["K001", "K002"])), "batch", who=TESTER
        )
        time.sleep(0.01)
        _append(export)
        first = await h.finish(changed)
        refused = await create_run(
            h.ctx, await h.spec(spec(table(), SERVICE, ["K001"], "denied")), "batch", who=TESTER
        )
        monkeypatch.setattr(duck.Files, "rows", denied)
        second = await h.finish(refused)
    assert (first["status"], first["error"]) == (
        "failed",
        "FilesChanged: side a: file 1 of 1 is not the file the run was created on (size)",
    )
    assert (second["status"], second["error"]) == ("failed", "TargetDenied: PermissionError")
    assert sum(service.calls.values()) == 0


async def test_a_change_no_stat_shows_is_met_by_the_closing_hash(
    opened: Opened, exports: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass's files are hashed again when its rows end, so other bytes of the same size, times and
    inode (here on a mount that keeps no change time) fail the run as changed files (content)."""
    _pair(exports)
    real = manifest._key
    monkeypatch.setattr(manifest, "_key", lambda found: (*real(found)[:2], 0, real(found)[3]))
    export = exports / "a" / "export.csv"
    async with opened() as h:
        run_id = await create_run(h.ctx, await h.spec(spec(table(), other(table()))), "batch", who=TESTER)
        before = export.stat()
        with export.open("r+") as written:
            written.write(HEADER + rows(1).replace("n1", "N1"))
        os.utime(export, ns=(before.st_atime_ns, before.st_mtime_ns))
        run = await h.finish(run_id)
    assert (run["status"], run["error"]) == (
        "failed",
        "FilesChanged: side a: file 1 of 1 is not the file the run was created on (content)",
    )


@pytest.mark.parametrize("where", [None, "status = 'A'"], ids=["every row", "a filter"])
async def test_rows_moved_fails(opened: Opened, exports: Path, where: str | None) -> None:
    """A side read to its end met another number of rows than its plan counted (here
    the stored count is made to disagree, as a parser that disagreed with itself would)."""
    _pair(exports, HEADER + rows(*range(1, 9)) + rows(9, 10, status="B"))
    b = table() if where is None else table(where=where)
    async with opened() as h:
        run_id = await create_run(h.ctx, await h.spec(spec(table(), other(b))), "batch", who=TESTER)
        counted = (await h.run(run_id))["params"]["tables"]["b"]["selected"]
        async with h.ctx.pool.connection() as conn:
            moved = (
                "UPDATE ds.runs SET params = jsonb_set(params, '{tables,b,selected}', to_jsonb(%(n)s::int))"
                " WHERE run_id = %(id)s"
            )
            await conn.execute(moved, {"n": 10_000_000, "id": run_id})
        run = await h.finish(run_id)
    assert counted == (10 if where is None else 8)
    assert (run["status"], run["error"]) == (
        "failed", f"RowsMoved: side b: {counted} rows were read where the plan counted 10,000,000",
    )  # fmt: skip
    assert notify.error_type(run["error"]) == "RowsMoved"


@pytest.mark.parametrize(
    ("raised", "said"),
    [
        (TargetTempSpace("OutOfMemoryException"), "TargetTempSpace: OutOfMemoryException"),
        (TargetDenied("PermissionError"), "TargetDenied: PermissionError"),
    ],
)
async def test_a_scan_s_failure_fails_the_run_by_its_class(
    opened: Opened, exports: Path, monkeypatch: pytest.MonkeyPatch, raised: Exception, said: str
) -> None:
    """Temporary space and a refused permission fail a scan's run at once, by the error's class
    and code (never DuckDB's text), which the alert's error_type reads."""
    _pair(exports)

    async def scan(self: duck.Files, *args: Any) -> Any:
        raise raised

    async with opened() as h:
        run_id = await create_run(h.ctx, await h.spec(spec(table(), other(table()))), "batch", who=TESTER)
        monkeypatch.setattr(duck.Files, "scan", scan)
        run = await h.finish(run_id)
    assert (run["status"], run["error"]) == ("failed", said)
    assert notify.error_type(run["error"]) == type(raised).__name__


async def test_a_sort_without_free_space_fails_the_run(
    opened: Opened, exports: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spill's free space is checked before a sort reads a byte."""
    _pair(exports)
    async with opened() as h:
        run_id = await create_run(h.ctx, await h.spec(spec(table(), other(table()))), "batch", who=TESTER)
        monkeypatch.setattr(duck, "_free", lambda path: 0)
        run = await h.finish(run_id)
    assert (run["status"], run["error"], run["scan"]["rows_a"]) == ("failed", "TargetTempSpace: spill", 0)


async def test_io_error_on_unchanged_bytes_fails_at_the_third_pass(
    opened: Opened, exports: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A read error at the same row of files that did not change (a bad block: here a stub
    at K005) is met by every pass, so the third fails the run though the first saved checkpoints."""
    _pair(exports)
    monkeypatch.setattr(scanning, "BATCH", 2)
    monkeypatch.setattr(duck, "BATCH", 2)
    real, met = duck.Cursor.batch, []

    async def batch(self: duck.Cursor, *args: Any, **kwargs: Any) -> Any:
        found = await real(self, *args, **kwargs)
        if any(row[0] >= "K005" for row in found):
            met.append(1)
            raise TargetRefused("IOException")
        return found

    async with opened() as h:
        run_id = await create_run(h.ctx, await h.spec(spec(table(), other(table()))), "batch", who=TESTER)
        monkeypatch.setattr(duck.Cursor, "batch", batch)
        with caplog.at_level(logging.WARNING, logger="deltasleuth.runner.worker"):
            run = await h.finish(run_id)
    assert (run["status"], run["error"]) == ("failed", "TargetRefused: IOException")
    assert len(met) == 3 and run["scan"]["checkpoint"] == 1  # saved by the first pass, which counts too
    said = [record.getMessage() for record in caplog.records if "scan of run" in record.getMessage()]
    assert said == [f"scan of run {run_id} resumes: IOException"] * 2 + [
        f"scan of run {run_id} failed: TargetRefused: IOException"
    ]


async def test_an_unmounted_directory_pauses_and_a_changed_file_fails_at_resume(
    opened: Opened, exports: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A root that cannot be listed pauses the pass (the log names the class, never a path),
    as often as it takes; the pass that resumes stat-checks first, and fails the run on a changed file."""
    _pair(exports)
    async with opened() as h:
        run_id = await create_run(h.ctx, await h.spec(spec(table(), other(table()))), "batch", who=TESTER)
        (exports / "a").rename(exports / "away")
        stop = asyncio.Event()
        worker = asyncio.create_task(run_worker(h.ctx, stop=stop))
        paused = f"scan of run {run_id} paused: OSError"
        try:
            with caplog.at_level(logging.WARNING, logger="deltasleuth.runner.worker"):
                async with asyncio.timeout(30):
                    while [record.getMessage() for record in caplog.records].count(paused) < 2:
                        await asyncio.sleep(0.05)
            waiting = await h.run(run_id)
            (exports / "away").rename(exports / "a")
            _append(exports / "a" / "export.csv")
            run = await h.wait(run_id)
        finally:
            stop.set()
            await worker
    assert (waiting["status"], waiting["scan"]["checkpoint"]) == ("running", 0)
    assert (run["status"], run["error"]) == (
        "failed",
        "FilesChanged: side a: file 1 of 1 is not the file the run was created on (size)",
    )
    assert str(exports) not in caplog.text


# --- a reader's process that ends ----------------------------------------------------


async def test_a_run_whose_reader_dies_three_times_fails_with_f12(
    opened: Opened, exports: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A side's files under whose statements the reader's process ends (as a fault ends
    it: here by a signal that is no crash to the system, sent as each is taken) pause a scan's pass twice,
    each end charged to them alone, and fail the run at the third; a key-list run of such files fails at
    its third read. The error is the closed text, naming the cause and no file, and the alert's error_type
    its class. The worker lives through the six ends, and a run of other files completes after them, on a
    new reader."""
    _pair(exports)
    for name in ("bad.csv", "worse.csv"):  # two file sets: each is charged its own ends
        (exports / "a" / name).write_text(HEADER + rows(*range(1, 11)))
    ask, faulty = duck.Instance.ask, []

    def faulting(self: duck.Instance, by: Any = None, /, **request: Any) -> Any:
        answer = ask(self, by, **request)
        if any(name in str(request.get("binds")) for name in faulty):
            with suppress(ProcessLookupError):
                os.kill(self.proc.pid, faults.QUIET)
        return answer

    monkeypatch.setattr(duck.Instance, "ask", faulting)
    listed = spec(table("worse.csv"), other(table()), ["K001", "K002"], "listed")
    clean = json.loads(spec(table(), other(table())))  # another version of the scanned pair: other files
    clean["metadata"]["version"] = "1.1.0"
    async with opened() as h:
        stop = asyncio.Event()
        worker = asyncio.create_task(run_worker(h.ctx, stop=stop))
        try:
            with caplog.at_level(logging.WARNING):
                scan_id = await create_run(
                    h.ctx, await h.spec(spec(table("bad.csv"), other(table()))), "batch", who=TESTER
                )
                faulty.append("bad.csv")
                scanned = await h.wait(scan_id)
                list_id = await create_run(h.ctx, await h.spec(listed), "batch", who=TESTER)
                faulty.append("worse.csv")
                by_key = await h.wait(list_id)
                clean_id = await create_run(h.ctx, await h.spec(json.dumps(clean)), "batch", who=TESTER)
                done = await h.wait(clean_id)
            alive = not worker.done()
        finally:
            stop.set()
            await worker
        found = await h.results(done["run_id"])
    f12 = (
        f"ReaderDied: side a: the file reader's process ended 3 times ({faults.QUIET.name}) while only this"
        " side's files were being read: they are read no more until DeltaSleuth restarts"
    )
    assert (scanned["status"], scanned["error"]) == (by_key["status"], by_key["error"]) == ("failed", f12)
    assert notify.error_type(scanned["error"]) == "ReaderDied" and scanned["scan"]["checkpoint"] == 0
    assert alive and done["status"] == "completed"
    assert {key: r["outcome"] for key, r in found.items()} == SEEDED
    said = [record.getMessage() for record in caplog.records]
    assert [line for line in said if line.startswith(f"scan of run {scan_id}")] == [
        f"scan of run {scan_id} paused: Died",
        f"scan of run {scan_id} paused: Died",
        f"scan of run {scan_id} failed: {f12}",
    ]
    ended = (
        f"the file reader's process ended ({faults.QUIET.name}) while only one side's files were being read:"
        " {} of the 3"
    )
    assert [line for line in said if line.startswith("the file reader's process")] == [
        f"{ended.format(n)} after which they are read no more" for n in (1, 2, 3, 1, 2, 3)
    ]
    assert f"run {list_id} failed: {f12}" in said
    said = [line for line in said if not line.startswith("the file reader started: duckdb ")]  # its starts
    assert not re.search(r"\.csv|export|worse|duckdb", "\n".join(said) + f12, re.IGNORECASE)
    assert str(exports) not in caplog.text


@pytest.mark.parametrize("code", ["Died", "Alone", "Unstarted"])
async def test_a_reader_that_takes_no_work_pauses_a_scan(
    opened: Opened,
    exports: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    code: str,
) -> None:
    """A reader that ended, did not start or reads another export apart is an outage with a
    code of its own: the pass is paused, logged by that code, and resumed."""
    _pair(exports)
    scan, met = duck.Files.scan, []

    async def refusing(self: duck.Files, *args: Any) -> Any:
        if not met:
            met.append(1)
            raise manifest.Busy(code)
        return await scan(self, *args)

    async with opened() as h:
        run_id = await create_run(h.ctx, await h.spec(spec(table(), other(table()))), "batch", who=TESTER)
        monkeypatch.setattr(duck.Files, "scan", refusing)
        with caplog.at_level(logging.WARNING, logger="deltasleuth.runner.worker"):
            run = await h.finish(run_id)
    assert run["status"] == "completed" and run["error"] is None
    said = [record.getMessage() for record in caplog.records if record.name == "deltasleuth.runner.worker"]
    assert said == [f"scan of run {run_id} paused: {code}"]


# --- a mount that does not answer ------------------------------------------------------------------------


async def test_a_mount_that_does_not_answer_costs_its_own_runs_only(
    opened: Opened,
    service: ScriptedService,
    exports: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stat check that hangs is abandoned at its deadline, never joined; each pass is an outage,
    paused and tried again, until four abandoned reads of its target's root wait: then that target's file
    work is refused at once, without another thread. A run without a file side goes on meanwhile, a run of
    another files target is created and read while the dead one stands at its cap, and the file run completes
    once the mount answers and the reads return."""
    _pair(exports)
    service.both("K001", doc("K001"), doc("K001"))
    service.script("b", "K002", (200, served(2)))
    hung, rooted = threading.Event(), manifest._rooted

    def hanging(root: str) -> str:
        if root == str(exports / "a"):  # fa's mount; fb's answers
            hung.wait()
        return rooted(root)

    healthy = json.loads(spec(table(), SERVICE, ["K002"]))  # another version of the pair: side a reads fb
    healthy["metadata"]["version"], healthy["sides"]["a"]["target"] = "1.1.0", "fb"
    async with opened() as h:
        run_id = await create_run(h.ctx, await h.spec(spec(table(), other(table()))), "batch", who=TESTER)
        beside = await create_run(h.ctx, await h.spec(spec_text(["K001"])), "batch", who=TESTER)
        healthy_id = await h.spec(json.dumps(healthy))
        monkeypatch.setattr(manifest, "STAT_S", 0.2)
        monkeypatch.setattr(manifest, "_rooted", hanging)
        stop = asyncio.Event()
        worker = asyncio.create_task(run_worker(h.ctx, stop=stop))
        refused = f"scan of run {run_id} paused: Stuck"
        try:
            with caplog.at_level(logging.WARNING):
                async with asyncio.timeout(60):
                    while refused not in [record.getMessage() for record in caplog.records]:
                        await asyncio.sleep(0.05)
                waiting, served_ = await h.run(run_id), await h.wait(beside)
                stuck = manifest.stuck(str(exports / "a"))
                # The dead mount's target is at its cap: the other files target is planned and read.
                fine = await h.wait(await create_run(h.ctx, healthy_id, "batch", who=TESTER))
                read_fb = await h.results(fine["run_id"])
                hung.set()
                run = await h.wait(run_id)
        finally:
            hung.set()
            stop.set()
            await worker
        found, read = await h.results(run_id), await h.results(beside)
    said = [record.getMessage() for record in caplog.records]
    assert stuck == manifest.STUCK_MAX == 4 and manifest.stuck() == 0
    assert [line for line in said if line.startswith("file read abandoned")] == [
        f"file read abandoned after its deadline ({n} waiting)" for n in (1, 2, 3, 4)
    ]
    assert f"scan of run {run_id} paused: Deadline" in said
    # While the mount hung, the run of two services was read and judged; the file run waited, running.
    assert (waiting["status"], served_["status"], read["K001"]["outcome"]) == (
        "running",
        "completed",
        "MATCH",
    )
    assert (fine["status"], read_fb["K002"]["outcome"]) == ("completed", "MATCH")
    assert run["status"] == "completed"
    assert {key: r["outcome"] for key, r in found.items()} == SEEDED


# --- privacy ----------------------------------------------------------------------------------


def _held(value: Any) -> list[Any]:
    """Every scalar a JSON value holds, its members' names among them."""
    if isinstance(value, dict):
        return [*value, *(found for item in value.values() for found in _held(item))]
    if isinstance(value, list):
        return [found for item in value for found in _held(item)]
    return [value]


async def test_per_file_digests_are_sealed(
    opened: Opened, exports: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A matched file's name, size, times, inode and digest are in the run's sealed pins alone:
    in no stored plan, plan.json, audit row, census record, answer or log. Only the aggregate files_sha256
    leaves, unkeyed, in the stored plan and plan.json, and in no answer or audit row; the pins are unreadable
    without the run's data key."""
    parts = {
        "part-0001-canary.csv": HEADER + rows(*range(1, 8)),
        "part-0002-canary.csv": HEADER + rows(8, 9, 10),
    }
    for name, data in parts.items():
        (exports / "a" / name).write_text(data)
    (exports / "b" / "export.csv").write_text(HEADER + rows(*range(1, 10)) + "short\n")
    a = table("*.csv")
    found = [(exports / "a" / name).stat() for name in parts]
    digests = [hashlib.sha256((exports / "a" / name).read_bytes()).hexdigest() for name in parts]
    secret = {v for f in found for v in (f.st_size, f.st_mtime_ns, f.st_ctime_ns, f.st_ino)} | set(digests)
    with caplog.at_level(logging.DEBUG):
        async with opened() as h:
            run_id = await create_run(h.ctx, await h.spec(spec(a, other(table()))), "batch", who=TESTER)
            run = await h.finish(run_id)
            async with h.ctx.pool.connection() as conn:
                listed, _ = await repo.list_runs(conn)
                audited = await conn.execute("SELECT detail FROM ds.audit_log WHERE object = %s", (run_id,))
                audit = [detail for (detail,) in await audited.fetchall()]
                ended = await repo.get_cycle(conn, run_id, 1)
                sealer = await h.ctx.sealer(conn, run_id)
            pins = tables.unsealed(sealer, run["params"])["a"]["pinned"]
    plan, aggregate = (
        run["params"]["tables"]["a"],
        hashlib.sha256("".join(f"{d}\n" for d in digests).encode()),
    )
    assert run["status"] == "completed" and set(run["params"]["pinned"]["a"]) == {"enc"}
    assert pins["files"] == list(parts) and [pin[4] for pin in pins["pins"]] == digests
    assert (plan["files_sha256"], plan["file_count"], plan["bytes"]) == (
        aggregate.hexdigest(), 2, sum(f.st_size for f in found),
    )  # fmt: skip
    packed = json.loads(
        approvals.plan(run["params"])
    )  # a pack's facts/plan.json, and what plan_sha256 hashes
    assert "pinned" not in packed and packed["tables"]["a"]["files_sha256"] == plan["files_sha256"]
    answers = [shown(run), shown(listed[0])]
    assert all("pinned" not in answer["params"] for answer in answers)
    stored = {"tables": run["params"]["tables"], "plan.json": packed, "audit": audit, "cycle": dict(ended)}
    for where, value in (stored | {"answers": answers}).items():
        held = _held(value)
        assert not secret & set(held), where
        assert not [text for text in held if isinstance(text, str) and "canary" in text], where
        if where not in ("tables", "plan.json"):  # the one digest that leaves, in the plan alone
            assert plan["files_sha256"] not in json.dumps(value, default=str), where
            assert "files_sha256" not in held, where
    logged = caplog.text
    assert "canary" not in logged and plan["files_sha256"] not in logged
    assert not [d for d in digests if d in logged]
    with pytest.raises(ValueError):  # another key opens nothing
        tables.unsealed(Sealer(os.urandom(32), run_id), run["params"])
