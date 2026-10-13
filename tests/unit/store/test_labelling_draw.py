"""A labelling's draw in the store: ``repo.labelling_draw``'s one statement equals
the Python it replaces over 10,000 synthetic results (``*`` items, non-ASCII keys, each population and
count), beside another run's rows; its joins do not follow the planner's estimates, which a new store's
first cycle end leaves wrong; coverage tops causes up in rank order and stops at 1,000 items; a stopped
draw stores nothing; a repeated draw, sequential or racing, returns the first labelling."""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections import Counter
from datetime import UTC
from types import SimpleNamespace
from typing import Any

import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from deltasleuth.core import sampling
from deltasleuth.core.types import ChangeType, CompiledSpec, Direction, RootCause, Severity
from deltasleuth.reports import accuracy
from deltasleuth.reports.csv import parse_csv
from deltasleuth.store import repo
from tests.conftest import TESTER
from tests.unit.store.four_eyes import counters, end, person, spec, start, upload

pytestmark = pytest.mark.db

SALT = "3f9a0c1e5b7d2468ace0135792468bdf"
TRUTH = {"source": "CBS-PROD.account_master", "as_of": "2026-01-01"}
CAUSES = 300
BUG, RULE = "KD-BUG-1", "KD-FX-1"
SIZES = {"size": 100, "per_cause": 2, "accepted": 50, "agreed": 80}


def cause(n: int) -> str:
    return f"RC-{n % CAUSES:012x}"


def key(i: int) -> str:
    """A masked key of a ``business_code`` class, shown in clear: every 97th holds non-ASCII letters."""
    return f"Ключ-ü{i:05d}" if i % 97 == 0 else f"K{i:05d}"


def synthetic(n: int) -> dict[str, tuple[str, list[tuple[str | None, str | None, str | None, bool]]]]:
    """``n`` results by masked key: its outcome and findings as (root cause, rule, classification,
    suppressed). Mismatches hold one or two root causes beside an INFO and a mask-suppressed finding without
    one; a NOT_FOUND's document finding is suppressed and a bug rule's finding is INFO, so both are ``*``."""
    found: dict[str, tuple[str, list[tuple[str | None, str | None, str | None, bool]]]] = {}
    for i in range(n):
        match i % 10:
            case 6:
                causes = {cause(i), cause(i // 7)} if i % 3 == 0 else {cause(i)}
                findings = [(c, None, None, False) for c in sorted(causes)]
                found[key(i)] = ("MISMATCH", [*findings, (None, None, None, False), (None, None, None, True)])
            case 7 if i % 20 == 7:
                found[key(i)] = ("KNOWN_DIFF", [(None, BUG, "bug", True)])  # a bug rule on an INFO finding
            case 7:
                found[key(i)] = ("KNOWN_DIFF", [(None, RULE, "accepted", True), (None, None, None, True)])
            case 8 if i % 20 == 8:
                found[key(i)] = ("NOT_FOUND_B", [(None, None, None, True)])
            case 8:
                found[key(i)] = ("NOISE_ONLY", [(None, None, None, True)])
            case 9:
                found[key(i)] = ("INCONCLUSIVE" if i % 20 == 9 else "DRIFT", [])
            case _:
                found[key(i)] = ("MATCH", [])
    return found


async def fill(
    conn: repo.Conn,
    run_id: str,
    results: dict[str, tuple[str, list[tuple[str | None, str | None, str | None, bool]]]],
    ranks: list[int],
) -> None:
    """The run's first cycle holding ``results`` (``synthetic``'s) and the root causes, ranked ``ranks``."""
    async with conn.cursor() as cur:
        await cur.executemany(
            """INSERT INTO ds.run_keys (run_id, cycle, key_masked, key_enc, seq, state)
               VALUES (%s, 1, %s, decode(repeat('01', 30), 'hex'), %s, 'done')""",
            [(run_id, masked, seq) for seq, masked in enumerate(results, start=10)],
        )
        await cur.executemany(
            """INSERT INTO ds.results (run_id, cycle, key_masked, outcome, side_a_state, side_b_state,
                   inconclusive_reason, drift_reason, confirmation, first_outcome)
               VALUES (%s, 1, %s, %s, 'COMPLETE', 'COMPLETE', %s, %s, 'rechecked', %s)""",
            [
                (run_id, masked, outcome, "timeout" if outcome == "INCONCLUSIVE" else None,
                 "changed_since" if outcome == "DRIFT" else None, outcome)
                for masked, (outcome, _) in results.items()
            ],
        )  # fmt: skip
        await cur.executemany(
            """INSERT INTO ds.findings (run_id, cycle, key_masked, seq, finding_id, severity, change_type,
                   direction, path_pattern, json_pointer, rule_id, rule_classification, suppressed,
                   root_cause_id)
               VALUES (%s, 1, %s, %s, 'F-' || substr(md5(%s), 1, 16), 'HIGH', 'value_changed', 'both',
                       '$.x', '/x', %s, %s, %s, %s)""",
            [
                (run_id, masked, seq, f"{run_id}/{masked}/{seq}", rule, classification, suppressed, found)
                for masked, (_, findings) in results.items()
                for seq, (found, rule, classification, suppressed) in enumerate(findings)
            ],
        )
        await cur.executemany(
            """INSERT INTO ds.root_causes (run_id, cycle, root_cause_id, seq, kind, max_severity,
                   path_pattern, change_type, direction, affected_keys, finding_count)
               VALUES (%s, 1, %s, %s, 'signature', 'HIGH', '$.x', 'value_changed', 'both', 1, 1)""",
            [(run_id, cause(c), seq) for c, seq in enumerate(ranks)],
        )


async def store(pool: AsyncConnectionPool, n: int, *, matched: int | None = None) -> tuple[str, CompiledSpec]:
    """An ended first cycle holding ``synthetic(n)`` beside the run's three keys without a result, its
    root causes ranked by number, and (``matched``) a scan that matched rows by row hash. The store holds
    another run of the same keys, each with the result of the key ten after it (a key a rule accepts here
    is under a bug rule there) and the root causes ranked in reverse: nothing of it may be drawn."""
    version = spec()
    async with pool.connection() as conn:
        amy = await person(conn, "amy", "operator")
        spec_id = await upload(conn, version, amy)
        results = synthetic(n)
        found = list(results.values())
        await fill(
            conn, await start(conn, spec_id, amy), dict(zip(results, found[10:] + found[:10], strict=True)),
            list(range(CAUSES))[::-1],
        )  # fmt: skip
        run_id = await start(conn, spec_id, amy)
        await fill(conn, run_id, results, list(range(CAUSES)))
        if matched is not None:
            state = Jsonb({"done": True, "matched": matched})
            await conn.execute(
                "INSERT INTO ds.scans (run_id, cycle, state) VALUES (%s, 1, %s)", (run_id, state)
            )
        await end(conn, run_id, counters(1))
    return run_id, version


def context(pool: AsyncConnectionPool, version: CompiledSpec) -> Any:
    """A context as ``store_draw`` reads it: the pool and the run's compiled spec."""

    async def compiled(conn: repo.Conn, spec_id: int) -> CompiledSpec:
        return version

    return SimpleNamespace(pool=pool, spec=compiled)


def expected(n: int, salt: str = SALT) -> dict[str, list[tuple[str, str]]]:
    """Each population's items (masked key, cause) in digest order, as a labelling draws them."""
    items: dict[str, list[tuple[str, str]]] = {name: [] for name in accuracy.POPULATIONS}
    for masked, (outcome, findings) in synthetic(n).items():
        causes = sorted({found for found, _, _, _ in findings if found})
        bug = any(classification == "bug" for _, _, classification, _ in findings)
        if outcome in ("MISMATCH", "NOT_FOUND_A", "NOT_FOUND_B") or (outcome == "KNOWN_DIFF" and bug):
            items["discrepant"] += [(masked, c) for c in causes or ["*"]]
        elif outcome in ("KNOWN_DIFF", "NOISE_ONLY"):
            items["accepted"].append((masked, "*"))
        elif outcome == "MATCH":
            items["agreed"].append((masked, "*"))
    return {
        name: sorted(found, key=lambda item: sampling.digest(salt, f"{item[0]}\x1f{item[1]}"))
        for name, found in items.items()
    }


async def test_sql_equals_python_over_10_000_synthetic_results(pool: AsyncConnectionPool) -> None:
    run_id, _ = await store(pool, 10_000, matched=123_456)
    async with pool.connection() as conn:
        drawn = await repo.labelling_draw(conn, run_id, 1, salt=SALT, timeout_s=30, **SIZES)
    want = expected(10_000)
    by_kind: dict[str, list[repo.Row]] = {}
    for row in drawn:
        by_kind.setdefault(row["kind"], []).append(row)
    for name, size in (("discrepant", 100), ("accepted", 50), ("agreed", 80)):
        got = sorted((r for r in by_kind["uniform"] if r["population"] == name), key=lambda r: r["digest"])
        assert [(r["key_masked"], r["cause"]) for r in got] == want[name][:size]
        assert all(
            bytes(r["digest"]) == sampling.digest(SALT, f"{r['key_masked']}\x1f{r['cause']}") for r in got
        )
    by_cause: dict[str, list[tuple[str, str]]] = {}
    for item in want["discrepant"]:
        by_cause.setdefault(item[1], []).append(item)
    coverage = {(r["key_masked"], r["cause"]) for r in by_kind["coverage"]}
    assert coverage == {item for c, found in by_cause.items() if c != "*" for item in found[:2]}
    sizes = {r["cause"]: (r["n"], r["seq"]) for r in by_kind["cause"]}
    assert sizes == {c: (len(found), None if c == "*" else int(c[3:], 16)) for c, found in by_cause.items()}
    outcomes = Counter(outcome for outcome, _ in synthetic(10_000).values())
    assert {r["population"]: r["n"] for r in by_kind["keys"]} == {
        "discrepant": len({masked for masked, _ in want["discrepant"]}),
        "accepted": len(want["accepted"]),
        "agreed": outcomes["MATCH"],
        "indeterminate": outcomes["INCONCLUSIVE"] + outcomes["DRIFT"],
    }
    assert [r["n"] for r in by_kind["not_compared"]] == [3]  # the run's own keys, never compared
    assert [r["n"] for r in by_kind["matched_by_row_hash"]] == [123_456]
    ruled = outcomes["KNOWN_DIFF"] - sum(1 for i in range(10_000) if i % 20 == 7)
    assert {r["cause"]: r["n"] for r in by_kind["rule"]} == {RULE: ruled, "mask": outcomes["NOISE_ONLY"]}
    # A key with root-caused findings beside an INFO or suppressed one gets only its causes; the keys
    # without any are one ``*`` item each, non-ASCII keys among both.
    starred = {masked for masked, c in want["discrepant"] if c == "*"}
    assert starred == {key(i) for i in range(10_000) if i % 20 in (7, 8)}
    assert any(not masked.isascii() for masked, _ in want["discrepant"][:400])
    async with pool.connection() as conn:  # nothing asked for, nothing read
        only = await repo.labelling_draw(
            conn, run_id, 1, salt=SALT, size=3, per_cause=0, accepted=0, agreed=0, timeout_s=30
        )
    assert Counter(r["kind"] for r in only if r["kind"] in ("uniform", "coverage", "rule")) == {"uniform": 3}


def _nodes(plan: dict[str, Any]) -> list[str]:
    return [plan["Node Type"], *(node for child in plan.get("Plans", []) for node in _nodes(child))]


async def test_a_new_stores_first_cycle_is_drawn_without_a_nested_loop_and_its_links_are_analysed(
    pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new store's first cycle end analyses its tables before any finding has a root cause
    (``repo.analyse_new``): the statistics then say none has one, the planner takes the cycle's linked
    findings for one row, and a nested loop met every result once per linked finding. The draw's joins are
    hash or merge joins whatever the statistics say, and ``replace_root_causes`` analyses its links."""
    n, results, plans, rows = 2000, synthetic(2000), [], repo._rows
    causes = [
        RootCause(cause(c), "signature", "$.x", ChangeType.VALUE_CHANGED, Direction.BOTH, 1, 1, Severity.HIGH)
        for c in range(CAUSES)
    ]
    null_frac = """SELECT null_frac FROM pg_stats
                   WHERE (schemaname, tablename, attname) = ('ds', 'findings', 'root_cause_id')"""

    async def explained(conn: repo.Conn, sql: Any, params: Any = None) -> list[dict[str, Any]]:
        found = await (await conn.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sql, params)).fetchone()
        plans.append(found[0][0]["Plan"])  # type: ignore[index]
        return await rows(conn, sql, params)

    async with pool.connection() as conn:
        await conn.execute("ALTER TABLE ds.findings SET (autovacuum_enabled = off)")  # none meanwhile
        amy = await person(conn, "amy", "operator")
        run_id = await start(conn, await upload(conn, spec(), amy), amy)
        unlinked = {
            masked: (outcome, [(None, *rest) for _, *rest in findings])
            for masked, (outcome, findings) in results.items()
        }
        await fill(conn, run_id, unlinked, [])  # type: ignore[arg-type]
    links = {
        "F-" + hashlib.md5(f"{run_id}/{masked}/{seq}".encode()).hexdigest()[:16]: found  # noqa: S324
        for masked, (_, findings) in results.items()
        for seq, (found, _, _, _) in enumerate(findings)
        if found
    }
    async with pool.connection() as conn:  # as cycle.finish_cycle: analysed, then linked and ended
        assert await repo.analyse_new(conn)
    async with pool.connection() as conn:
        assert await repo._value(conn, null_frac, None) == 1
        await conn.execute(  # the links without their analysis: the statistics a draw may meet
            """UPDATE ds.findings f SET root_cause_id = m.rc
               FROM unnest(%s::text[], %s::text[]) AS m (id, rc) WHERE f.finding_id = m.id""",
            (list(links), list(links.values())),
        )
        await end(conn, run_id, counters(1))
    monkeypatch.setattr(repo, "_rows", explained)
    async with pool.connection() as conn:
        drawn = await repo.labelling_draw(conn, run_id, 1, salt=SALT, timeout_s=30, **SIZES)
    monkeypatch.setattr(repo, "_rows", rows)
    assert "Nested Loop" not in _nodes(plans[0]) and "Hash Join" in _nodes(plans[0])
    uniform = sorted((r for r in drawn if r["kind"] == "uniform"), key=lambda r: r["digest"])
    assert [(r["key_masked"], r["cause"]) for r in uniform if r["population"] == "discrepant"] == (
        expected(n)["discrepant"][:100]
    )
    async with pool.connection() as conn:
        await repo.replace_root_causes(conn, run_id, 1, causes, links)
        assert 0 < await repo._value(conn, null_frac, None) < 1
        await repo.replace_root_causes(conn, run_id, 1, causes, links)  # analysed: not again
    async with pool.connection() as conn:
        analysed = await repo._value(
            conn, "SELECT analyze_count FROM pg_stat_user_tables WHERE relid = 'ds.findings'::regclass", None
        )
    assert analysed == 2  # analyse_new's and the first links'


async def test_coverage_in_rank_order_stops_at_1_000_items_with_the_causes_left_out(
    pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, version = await store(pool, 10_000)
    monkeypatch.setattr(sampling, "nonce", lambda: SALT)
    row, again = await accuracy.store_draw(
        context(pool, version), run_id, 1, who=TESTER, size=500, per_cause=20, accepted=100, agreed=100,
        truth=TRUTH,
    )  # fmt: skip
    selection, items = row["selection"], row["selection"]["items"]
    assert again is None and row["salt"] == SALT and len(items) == accuracy.MAX_ITEMS == 1000
    assert [i["item"] for i in items] == list(range(1, 1001))
    want = expected(10_000)
    # The uniform items, then each cause in rank order topped up to its 20 smallest until the 1,000th item.
    chosen = {item: "uniform" for item in want["discrepant"][:500]}
    for n in range(CAUSES):
        for item in [item for item in want["discrepant"] if item[1] == cause(n)][:20]:
            if item not in chosen and len(chosen) < 800:
                chosen[item] = "coverage"
    discrepant = [(item, chosen[item]) for item in want["discrepant"] if item in chosen]
    listed = [((i["key"], i["root_cause_id"]), i["selected_for"]) for i in items]
    assert listed[:800] == discrepant and Counter(why for _, why in discrepant) == {
        "uniform": 500,
        "coverage": 300,
    }
    assert listed[800:900] == [(item, "uniform") for item in want["accepted"][:100]]
    assert listed[900:] == [(item, "uniform") for item in want["agreed"][:100]]
    assert [i["population"] for i in items] == ["discrepant"] * 800 + ["accepted"] * 100 + ["agreed"] * 100
    # Each cause holds its smallest digests (its bottom m_h); those ranked after the limit hold only what
    # the uniform draw gave them, and the ones it gave nothing have no item of their own.
    held: dict[str, list[tuple[str, str]]] = {}
    for item, _ in discrepant:
        held.setdefault(item[1], []).append(item)
    for found, own in held.items():
        assert own == [item for item in want["discrepant"] if item[1] == found][: len(own)]
    causes = selection["causes"]
    assert [c["root_cause_id"] for c in causes] == [cause(n) for n in range(CAUSES) if cause(n) in held]
    assert [c["rank"] for c in causes] == [int(c["root_cause_id"][3:], 16) + 1 for c in causes]
    assert all(
        c["items"] == sum(item[1] == c["root_cause_id"] for item in want["discrepant"]) for c in causes
    )
    observed = len({found for _, found in want["discrepant"]} - {"*"})  # the causes with an item
    assert selection["uncovered"] == observed - len(causes) > 0
    assert selection["counts"]["discrepant"] == {
        "items": len(want["discrepant"]), "keys": len({masked for masked, _ in want["discrepant"]}),
        "causes": observed, "drawn": 800,
    }  # fmt: skip
    assert (
        f"per_cause 20 covered {len(causes)} of {observed} root causes within the 1,000 items a labelling"
        f" holds; {observed - len(causes)} have no item of their own"
    ) in selection["warnings"]
    header, rows = parse_csv(row["sheet"].encode())
    assert header == list(accuracy.SHEET_COLUMNS) and [r["key"] for r in rows] == [i["key"] for i in items]
    assert row["selection_sha256"] == accuracy.selection_sha256(items)


async def test_a_stopped_draw_is_a_timeout_and_stores_nothing(
    pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, version = await store(pool, 100)
    monkeypatch.setattr(accuracy, "DRAW_TIMEOUT_S", 1)
    async with pool.connection() as held:  # the draw's statement waits for this lock, past its timeout
        await held.execute("LOCK TABLE ds.findings IN ACCESS EXCLUSIVE MODE")
        with pytest.raises(accuracy.DrawTimeout) as stopped:
            await accuracy.store_draw(context(pool, version), run_id, 1, who=TESTER, truth=TRUTH)
    assert str(stopped.value) == (
        f"the draw of cycle 1 of run {run_id} was stopped after 1 s (QueryCanceled): nothing was drawn; draw"
        " again with accepted and agreed at 0, or when the store is less busy"
    )
    async with pool.connection() as conn:
        assert await repo.list_labellings(conn, run_id) == []
        assert await repo.last_audit(conn, "accuracy.draw") is None


async def test_a_repeated_draw_sequential_or_racing_returns_the_first(pool: AsyncConnectionPool) -> None:
    run_id, version = await store(pool, 1000)
    ctx = context(pool, version)
    first, again = await accuracy.store_draw(ctx, run_id, 1, who=TESTER, truth=TRUTH)
    second, warned = await accuracy.store_draw(ctx, run_id, 1, who=TESTER, truth=TRUTH)
    assert again is None and second["labelling_id"] == first["labelling_id"]
    assert warned == (
        f"labelling {first['labelling_id']} with these parameters was drawn by you at"
        f" {first['drawn_at'].astimezone(UTC):%H:%M:%S} UTC and is returned, not drawn again"
    )
    # Other sizes, another truth or another actor draw anew; so does the same draw once the first is older.
    async with pool.connection() as conn:
        bob = await person(conn, "bob", "operator")
    others = [
        await accuracy.store_draw(ctx, run_id, 1, who=TESTER, size=99, truth=TRUTH),
        await accuracy.store_draw(ctx, run_id, 1, who=TESTER, truth=TRUTH | {"as_of": "2025-12-31"}),
        await accuracy.store_draw(ctx, run_id, 1, who=bob, truth=TRUTH),
    ]
    assert all(warning is None for _, warning in others)
    async with pool.connection() as conn:
        await conn.execute("ALTER TABLE ds.labellings DISABLE TRIGGER labellings_lifecycle")
        await conn.execute(
            "UPDATE ds.labellings SET drawn_at = now() - interval '601 seconds' WHERE labelling_id = %s",
            (first["labelling_id"],),
        )
        await conn.execute("ALTER TABLE ds.labellings ENABLE TRIGGER labellings_lifecycle")
    later, warned = await accuracy.store_draw(ctx, run_id, 1, who=TESTER, truth=TRUTH)
    assert warned is None and later["labelling_id"] != first["labelling_id"]
    # A retry racing the first draw (a proxy timed out; the server finishes the draw): one labelling.
    truth = TRUTH | {"source": "CBS-PROD.raced"}
    raced = await asyncio.gather(
        *(accuracy.store_draw(ctx, run_id, 1, who=TESTER, size=7, truth=truth) for _ in range(4))
    )
    assert len({row["labelling_id"] for row, _ in raced}) == 1
    assert sorted(warning is None for _, warning in raced) == [False, False, False, True]
    async with pool.connection() as conn:
        rows = await repo.list_labellings(conn, run_id, 1)
        audited = await repo.audit_chain(conn, after=0, actions=["accuracy.draw"])
    assert len(rows) == len(audited) == 6  # a repeat audits nothing


@pytest.mark.skipif(not os.environ.get("DS_PERF"), reason="DS_PERF=1 runs it")
@pytest.mark.parametrize(("results", "seconds"), [(1_000_000, 10), (10_000_000, 120)])
async def test_perf_the_draw_of_a_large_cycle(pool: AsyncConnectionPool, results: int, seconds: int) -> None:
    """A cycle of ``results`` discrepant keys, one root cause each, in a new store that holds another run
    a tenth its size: drawn within ``seconds``. The store is seeded in the cycle end's order: its tables
    are analysed before any finding has a root cause (``repo.analyse_new``), then ``replace_root_causes``
    links them, and nothing analyses by hand."""
    causes = [
        RootCause(cause(c), "signature", "$.x", ChangeType.VALUE_CHANGED, Direction.BOTH, 1, 1, Severity.HIGH)
        for c in range(CAUSES)
    ]
    async with pool.connection() as conn:
        amy = await person(conn, "amy", "operator")
        spec_id = await upload(conn, spec(), amy)
        other, run_id = [await start(conn, spec_id, amy) for _ in range(2)]
    async with pool.connection() as conn:
        # Without the foreign-key checks, whose plan reads an unanalysed table's rows one by one.
        await conn.execute("SET LOCAL session_replication_role = replica")
        for seeded, n in ((other, results // 10), (run_id, results)):
            await conn.execute(
                """INSERT INTO ds.run_keys (run_id, cycle, key_masked, key_enc, seq, state)
                   SELECT %s, 1, 'm' || g, decode(repeat('01', 30), 'hex'), g + 10, 'done'
                   FROM generate_series(1, %s) g""",
                (seeded, n),
            )
            await conn.execute(
                """INSERT INTO ds.results (run_id, cycle, key_masked, outcome, side_a_state, side_b_state,
                       confirmation, first_outcome)
                   SELECT %s, 1, 'm' || g, 'MISMATCH', 'COMPLETE', 'COMPLETE', 'rechecked', 'MISMATCH'
                   FROM generate_series(1, %s) g""",
                (seeded, n),
            )
            await conn.execute(
                """INSERT INTO ds.findings (run_id, cycle, key_masked, seq, finding_id, severity, change_type,
                       direction, path_pattern, json_pointer, rule_id, rule_classification, suppressed)
                   SELECT %s, 1, 'm' || g, s, 'F-' || substr(md5(%s || g || '/' || s), 1, 16), 'HIGH',
                          'value_changed', 'both', '$.x', '/x', CASE WHEN s = 1 THEN %s END,
                          CASE WHEN s = 1 THEN 'bug' END, s = 1
                   FROM generate_series(1, %s) g, generate_series(0, 1) s""",
                (seeded, seeded, BUG, n),
            )
    async with pool.connection() as conn:  # as cycle.finish_cycle: analysed, then linked and ended
        await repo.analyse_new(conn)
    async with pool.connection() as conn:
        found = await conn.execute(
            """SELECT finding_id, 'RC-' || lpad(to_hex(substr(key_masked, 2)::int %% %s), 12, '0')
               FROM ds.findings WHERE run_id = %s AND seq = 0""",
            (CAUSES, run_id),
        )
        links = dict(await found.fetchall())
        async with conn.transaction():
            await repo.replace_root_causes(conn, run_id, 1, causes, links)
            await end(conn, run_id, counters(1))
    started = time.monotonic()
    async with pool.connection() as conn:
        drawn = await repo.labelling_draw(conn, run_id, 1, salt=SALT, timeout_s=300, **SIZES)
    assert time.monotonic() - started < seconds
    assert sum(r["kind"] == "uniform" for r in drawn) == 100
    assert {r["cause"]: r["n"] for r in drawn if r["kind"] == "cause"} == Counter(links.values())
