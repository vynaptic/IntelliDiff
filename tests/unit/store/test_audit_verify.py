"""``ds audit verify`` and a pack's segment (docs/mvp/PACK.md §4.2, §4.3): an intact log verifies and
prints its head; each tampering a database owner can do is caught, naming the first row that fails:
an edited ``detail`` or ``text``, a deleted, unchained or SIEM-only row, a swapped ``obj_prev``, a
consistent rewrite (against the SIEM), a pre-009 row unlike its line, an edit covered by a replaced
``ds.audit_text``, two SIEM lines of one row that differ."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from psycopg_pool import AsyncConnectionPool
from typer.testing import CliRunner

from deltasleuth.auth.principal import WORKER
from deltasleuth.cli.main import app
from deltasleuth.store import chain, repo
from deltasleuth.store.chain import ChainBroken
from deltasleuth.store.db import migrate
from tests.conftest import TESTER

pytestmark = pytest.mark.db

ALTERED = "the audit log was altered after it was written"
ZERO = "0" * 64
# ds.audit_text as the database owner can replace it to cover an edit: a chained row's stored text.
COVER = """CREATE OR REPLACE FUNCTION ds.audit_text(a ds.audit_log) RETURNS text LANGUAGE sql STABLE AS $$
    SELECT c.text FROM ds.audit_chain c WHERE c.id = a.id $$"""
TARGETS = """\
targets:
  svc: {base_url: "http://127.0.0.1:9/", allow_private_network: true, allow_insecure_http: true}
"""


@pytest.fixture
def siem() -> Iterator[list[str]]:
    """The SIEM lines logged on ``deltasleuth.audit``."""
    lines: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: lines.append(record.getMessage())  # type: ignore[method-assign]
    logger = logging.getLogger("deltasleuth.audit")
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield lines
    logger.removeHandler(handler)
    logger.setLevel(level)


@pytest.fixture
async def log(empty_pool: AsyncConnectionPool, siem: list[str]) -> AsyncIterator[AsyncConnectionPool]:
    """A store at 009 whose SIEM lines are all logged, holding rows 1 to 7: system.migrate, then rows
    of R-1, R-2, R-1, R-2, R-1 and 1 (010 would add installation.created before them)."""
    await migrate(empty_pool, upto=9)
    for who, action, obj in (
        (TESTER, "create_run", "R-1"), (TESTER, "create_run", "R-2"), (WORKER, "run.cycle", "R-1"),
        (WORKER, "run.finished", "R-2"), (TESTER, "key.view", "R-1"), (TESTER, "spec.create", "1"),
    ):  # fmt: skip
        async with repo.audited(empty_pool) as (_, audit):
            await audit(who, action, obj, {"n": len(siem)})
    yield empty_pool


async def _verify(pool: AsyncConnectionPool, lines: list[str] | None = None, tmp: Path | None = None) -> Any:
    siem = None
    if lines is not None:
        assert tmp is not None
        (tmp / "siem.jsonl").write_text("".join(f"{line}\n" for line in lines))
        siem = chain.read_siem(tmp / "siem.jsonl")
    async with pool.connection() as conn:
        return await chain.verify(conn, siem)


async def _broken(pool: AsyncConnectionPool, lines: list[str] | None = None, tmp: Path | None = None) -> str:
    with pytest.raises(ChainBroken) as broken:
        await _verify(pool, lines, tmp)
    return str(broken.value)


async def _owner(pool: AsyncConnectionPool, sql: str, *params: Any) -> None:
    """``sql`` as the database owner can run it: around the append-only, chain and foreign key triggers."""
    async with pool.connection() as conn:
        await conn.execute("SET LOCAL session_replication_role = replica")
        await conn.execute(sql.encode(), params)


async def _rechain(
    pool: AsyncConnectionPool, first: int, obj_prev: Callable[[int, str], str] | None = None
) -> None:
    """Rewrite the chain from row ``first`` on as a database owner covering an edit would: each text
    from its audit row's columns and each link and hash recomputed. ``obj_prev(seq, prev)``, when given,
    replaces a row's object link."""
    async with pool.connection() as conn:
        await conn.execute("SET LOCAL session_replication_role = replica")
        cur = await conn.execute(
            "SELECT c.seq, c.object, ds.audit_text(l) FROM ds.audit_chain c JOIN ds.audit_log l USING (id)"
            " ORDER BY c.seq"
        )
        prev, last = ZERO, {}
        for seq, obj, text in await cur.fetchall():
            link = last.get(obj, ZERO)
            if obj_prev is not None and seq == first:
                link = obj_prev(seq, prev)
            digest = chain.chain_hash(prev, link, text)
            if seq >= first:
                await conn.execute(
                    "UPDATE ds.audit_chain SET prev = %s, obj_prev = %s, text = %s, hash = %s WHERE seq = %s",
                    (prev, link, text, digest, seq),
                )
            prev = last[obj] = digest


async def test_an_intact_log_verifies_against_itself_and_its_siem(
    log: AsyncConnectionPool, siem: list[str], tmp_path: Path
) -> None:
    alone, against = await _verify(log), await _verify(log, siem, tmp_path)
    async with log.connection() as conn:
        head = await repo.audit_head(conn)
    assert alone == {"head": head, "rows": 7, "upgrade_seq": 0, "siem": None}
    assert against == alone | {"siem": {"lines": 7, "first": 1, "last": 7}}
    assert head is not None and head["seq"] == 7


@pytest.mark.parametrize(
    ("tampering", "said"),
    [
        (
            ["UPDATE ds.audit_log SET detail = '{\"n\": 99}' WHERE id = 4"],
            f"row 4 (run.cycle R-1) does not hash to its chain entry: {ALTERED}",
        ),
        (  # the text of another row, its hash recomputed so that the CHECK holds
            [
                "UPDATE ds.audit_chain SET text = replace(text, '\"n\": 2', '\"n\": 5'),"
                " hash = ds.chain_hash(prev, obj_prev, replace(text, '\"n\": 2', '\"n\": 5')) WHERE seq = 3"
            ],
            f"row 3 (create_run R-2) does not hash to its chain entry: {ALTERED}",
        ),
        (
            ["DELETE FROM ds.audit_chain WHERE seq = 5", "DELETE FROM ds.audit_log WHERE id = 5"],
            f"row 5 is missing: {ALTERED}",
        ),
        (
            ["DELETE FROM ds.audit_chain WHERE seq IN (4, 5)", "DELETE FROM ds.audit_log WHERE id IN (4, 5)"],
            f"rows 4 to 5 are missing: {ALTERED}",
        ),
        (  # written around the chain trigger
            [
                "INSERT INTO ds.audit_log (id, actor, action, object)"
                " VALUES (8, 'system:worker', 'run.cycle', 'R-1')"
            ],
            f"the audit row with id 8 (run.cycle R-1) has no chain entry: {ALTERED}",
        ),
        (  # the last row's chain entry deleted: its audit row is left unchained
            ["DELETE FROM ds.audit_chain WHERE seq = 7"],
            f"the audit row with id 7 (spec.create 1) has no chain entry: {ALTERED}",
        ),
    ],
    ids=[
        "edited detail",
        "edited text",
        "deleted row",
        "deleted rows",
        "unchained row",
        "unchained last row",
    ],
)
async def test_each_tampering_is_caught(log: AsyncConnectionPool, tampering: list[str], said: str) -> None:
    for sql in tampering:
        await _owner(log, sql)
    assert await _broken(log) == said


async def test_an_edit_covered_by_one_rehashed_row_breaks_the_next_row(log: AsyncConnectionPool) -> None:
    await _owner(log, "UPDATE ds.audit_log SET detail = '{\"n\": 99}' WHERE id = 4")
    await _owner(
        log,
        "UPDATE ds.audit_chain SET text = ds.audit_text(l),"
        " hash = ds.chain_hash(prev, obj_prev, ds.audit_text(l))"
        " FROM ds.audit_log l WHERE l.id = 4 AND seq = 4",
    )
    assert await _broken(log) == f"row 5 (run.finished R-2) does not follow row 4: {ALTERED}"


@pytest.mark.parametrize(
    ("seq", "said"),
    [
        (6, f"row 6 (key.view R-1) does not follow row 4, the previous row of R-1: {ALTERED}"),
        (7, f"row 7 (spec.create 1) follows a row of 1 that is not in the chain: {ALTERED}"),
    ],
)
async def test_a_swapped_obj_prev_is_caught_though_every_hash_agrees(
    log: AsyncConnectionPool, seq: int, said: str
) -> None:
    await _rechain(log, seq, lambda _, prev: prev)  # the row before it given as its object's previous row
    assert await _broken(log) == said


async def test_an_edit_covered_by_a_replaced_audit_text_is_caught(
    log: AsyncConnectionPool, siem: list[str], tmp_path: Path
) -> None:
    async with repo.audited(log) as (_, audit):
        await audit(TESTER, "approval.grant", "A-1", {"step": 1, "of": 2})
    lines = list(siem)
    await _owner(
        log,
        "UPDATE ds.audit_log SET actor = 'user:U-ffffffffffffffff', actor_name = 'mallory',"
        " detail = jsonb_set(detail, '{step}', '2') WHERE id = 8",
    )
    await _owner(log, COVER)
    async with log.connection() as conn:  # the text every row's columns now give in SQL is the stored one
        cur = await conn.execute(
            "SELECT bool_and(c.text = ds.audit_text(l)) FROM ds.audit_chain c JOIN ds.audit_log l USING (id)"
        )
        assert await cur.fetchone() == (True,)
        segment = await repo.audit_segment(conn, ["A-1"], 8)
    said = f"row 8 (approval.grant A-1) does not hash to its chain entry: {ALTERED}"
    assert await _broken(log) == said
    assert await _broken(log, lines, tmp_path) == said
    with pytest.raises(ChainBroken) as broken:
        chain.check_segment(segment)
    assert (str(broken.value), broken.value.seq) == (said, 8)


@pytest.mark.parametrize(
    "edit",
    [
        "SET at = at + interval '1 microsecond'",
        "SET client_addr = '10.1.2.3'",
        "SET object = 'R-2'",
        "SET session_id = 'W-0123456789abcdef'",
    ],
    ids=["at", "client_addr", "object", "session_id"],
)
async def test_each_column_is_compared_with_the_stored_text(log: AsyncConnectionPool, edit: str) -> None:
    await _owner(log, f"UPDATE ds.audit_log {edit} WHERE id = 4")
    await _owner(log, COVER)
    assert await _broken(log) == f"row 4 (run.cycle R-1) does not hash to its chain entry: {ALTERED}"


async def test_a_chain_entry_without_its_audit_row_is_caught(log: AsyncConnectionPool) -> None:
    await _owner(log, "DELETE FROM ds.audit_log WHERE id = 4")
    said = await _broken(log)
    assert said.startswith("row 4 (") and said.endswith(f"does not hash to its chain entry: {ALTERED}")


async def test_a_masked_address_and_the_session_time_zone_raise_no_alarm(
    log: AsyncConnectionPool, siem: list[str], tmp_path: Path
) -> None:
    async with log.connection() as conn:  # written with psql: its text drops the mask its SIEM line keeps
        cur = await conn.execute(
            "INSERT INTO ds.audit_log (at, actor, action, object, client_addr)"
            " VALUES ('2026-09-28 03:32:03.4+02:30', 'system:worker', 'run.cycle', 'R-1', '10.1.2.3/24')"
            " RETURNING id"
        )
        [row] = await repo.audit_rows(conn, [(await cur.fetchone())[0]])  # type: ignore[index]
    line = repo.siem_line(row)
    assert '"client_addr": "10.1.2.3/24"' in line
    async with log.connection() as conn:
        await conn.execute("SET TimeZone = 'America/St_Johns'")
        await conn.commit()
        assert (await chain.verify(conn))["rows"] == 8
    assert (await _verify(log, [*siem, line], tmp_path))["siem"] == {"lines": 8, "first": 1, "last": 8}


async def test_a_siem_line_unlike_its_row_is_caught_though_its_chain_entry_agrees(
    log: AsyncConnectionPool, siem: list[str], tmp_path: Path
) -> None:
    lines = list(siem)
    forged = json.dumps(json.loads(lines[3]) | {"actor": "user:U-ffffffffffffffff", "actor_name": "mallory"})
    assert (
        await _broken(log, [*lines, forged], tmp_path)
        == "row 4 (run.cycle R-1) has two SIEM lines that differ: one is forged"
    )
    assert (
        await _broken(log, [*lines[:3], forged, *lines[4:]], tmp_path)
        == f"row 4 (run.cycle R-1) differs from its SIEM line: {ALTERED}"
    )
    delivered_twice = await _verify(log, [*lines, lines[3]], tmp_path)
    assert delivered_twice["siem"] == {"lines": 7, "first": 1, "last": 7}


async def test_line_separators_in_a_row_s_text_end_no_siem_line(
    log: AsyncConnectionPool, siem: list[str], tmp_path: Path
) -> None:
    async with repo.audited(log) as (_, audit):  # raw in its SIEM line, where str.splitlines() would split
        await audit(TESTER, "retention.hold", "R-1", {"reason": "hold\u2028pasted\u2029from\u0085notes"})
    assert "\u2028" in siem[-1] and "\u0085" in siem[-1]
    assert (await _verify(log, siem, tmp_path))["siem"] == {"lines": 8, "first": 1, "last": 8}


async def test_a_consistent_rewrite_is_caught_only_against_the_siem(
    log: AsyncConnectionPool, siem: list[str], tmp_path: Path
) -> None:
    lines = list(siem)
    await _owner(log, "UPDATE ds.audit_log SET detail = '{\"n\": 99}' WHERE id = 4")
    await _rechain(log, 4)
    assert (await _verify(log))["rows"] == 7  # consistent: only the SIEM shows it
    assert (
        await _broken(log, lines, tmp_path) == f"row 4 (run.cycle R-1) differs from its SIEM line: {ALTERED}"
    )


async def test_a_dropped_last_row_is_caught_only_against_the_siem(
    log: AsyncConnectionPool, siem: list[str], tmp_path: Path
) -> None:
    lines = list(siem)
    await _owner(log, "DELETE FROM ds.audit_chain WHERE seq = 7")
    await _owner(log, "DELETE FROM ds.audit_log WHERE id = 7")
    assert (await _verify(log))["rows"] == 6
    said = await _broken(log, lines, tmp_path)
    assert said == f"row 7 (spec.create 1) is in the SIEM but not in the chain: {ALTERED}"


async def test_a_row_without_a_siem_line_is_caught_within_the_lines_span(
    log: AsyncConnectionPool, siem: list[str], tmp_path: Path
) -> None:
    lines = list(siem)
    async with log.connection() as conn:  # a row written with psql: chained, but never a SIEM line
        await conn.execute(
            "INSERT INTO ds.audit_log (actor, action, object) VALUES ('system:worker', 'run.cycle', 'R-1')"
        )
    later = await _verify(log, lines, tmp_path)  # after the export: outside its span
    assert later["siem"] == {"lines": 7, "first": 1, "last": 7} and later["rows"] == 8
    async with repo.audited(log) as (_, audit):
        await audit(TESTER, "spec.create", "2")
    lost = "has no SIEM line: it was not written by DeltaSleuth, or the SIEM lost it"
    assert await _broken(log, [*lines, siem[-1]], tmp_path) == f"row 8 (run.cycle R-1) {lost}"
    assert await _broken(log, lines[:3] + lines[4:], tmp_path) == f"row 4 (run.cycle R-1) {lost}"  # lost one
    assert (await _verify(log, lines[2:6], tmp_path))["siem"] == {"lines": 4, "first": 3, "last": 6}
    forged = json.loads(lines[3])
    forged["chain"]["hash"] = "f" * 64
    said = await _broken(log, [*lines, json.dumps(forged)], tmp_path)
    assert said == "row 4 (run.cycle R-1) has two SIEM lines that differ: one is forged"


@pytest.fixture
async def log_008(empty_pool: AsyncConnectionPool, siem: list[str]) -> AsyncIterator[AsyncConnectionPool]:
    """A store at 008 holding rows written and logged by the release before the chain: system.migrate,
    then rows of R-1, R-2 and R-1."""
    await migrate(empty_pool, upto=8)
    for obj in ("R-1", "R-2", "R-1"):
        async with repo.audited(empty_pool) as (_, audit):
            await audit(TESTER, "create_run", obj, {"keys": 1})
    yield empty_pool


@pytest.mark.parametrize(
    ("tampering", "said"),
    [
        (None, None),
        (
            "UPDATE ds.audit_log SET detail = '{\"keys\": 2}' WHERE id = 3",
            f"row 3 (create_run R-2), chained at the upgrade, differs from its SIEM line: {ALTERED}",
        ),
        (
            "DELETE FROM ds.audit_log WHERE id = 3",
            f"the audit row with id 3 (create_run R-2) is in the SIEM but not in the log: {ALTERED}",
        ),
    ],
    ids=["intact", "altered before the upgrade", "deleted before the upgrade"],
)
async def test_rows_before_009_are_checked_against_their_lines_by_content(
    log_008: AsyncConnectionPool, siem: list[str], tmp_path: Path, tampering: str | None, said: str | None
) -> None:
    lines = list(siem)
    if tampering is not None:
        await _owner(log_008, tampering)
    assert await migrate(log_008, upto=9) == [9]
    upgraded = 4 if tampering is None or "UPDATE" in tampering else 3
    alone = await _verify(log_008)  # chained as they were found
    assert (alone["rows"], alone["upgrade_seq"]) == (upgraded + 1, upgraded)
    if said is None:
        against = await _verify(log_008, [*lines, siem[-1]], tmp_path)  # and system.migrate [9], chained
        assert against["siem"] == {"lines": 5, "first": 1, "last": 5}
    else:
        assert await _broken(log_008, lines, tmp_path) == said


async def test_two_lines_of_one_row_before_009_must_agree(
    log_008: AsyncConnectionPool, siem: list[str], tmp_path: Path
) -> None:
    lines = list(siem)
    assert await migrate(log_008, upto=9) == [9]
    async with log_008.connection() as conn:
        [row] = await repo.audit_rows(conn, [3])
    forged = json.dumps(json.loads(lines[2]) | {"detail": {"keys": 2}})
    said = await _broken(log_008, [*lines, forged], tmp_path)
    assert said == "the audit row with id 3 (create_run R-2) has two SIEM lines that differ: one is forged"
    chained = json.dumps(json.loads(lines[2]) | {"chain": row["chain"]})  # the same row, a chain entry added
    said = await _broken(log_008, [*lines, chained], tmp_path)
    assert said == "row 3 (create_run R-2) has two SIEM lines that differ: one is forged"


async def test_a_segment_holds_its_objects_rows_from_their_first_up_to_its_head(
    log: AsyncConnectionPool,
) -> None:
    async with log.connection() as conn:
        segment = await repo.audit_segment(conn, ["R-1", "1"], 6)
    assert [(r["seq"], r["object"], r["action"], r["audit_id"]) for r in segment] == [
        (2, "R-1", "create_run", 2), (4, "R-1", "run.cycle", 4), (6, "R-1", "key.view", 6),
    ]  # fmt: skip
    chain.check_segment(segment)
    await _owner(log, "DELETE FROM ds.audit_chain WHERE seq = 4")
    await _owner(log, "DELETE FROM ds.audit_log WHERE id = 4")
    async with log.connection() as conn:
        segment = await repo.audit_segment(conn, ["R-1", "R-2"], 7)
    with pytest.raises(ChainBroken) as broken:
        chain.check_segment(segment)
    assert (str(broken.value), broken.value.seq) == (
        f"row 6 (key.view R-1) does not follow row 2, the previous row of R-1: {ALTERED}",
        6,
    )


# -- the command ------------------------------------------------------------------------------------


@pytest.fixture
def audit_logger() -> Iterator[None]:
    """``deltasleuth.audit`` restored after a command attached its SIEM copy to it."""
    logger = logging.getLogger("deltasleuth.audit")
    handlers, propagate, level = logger.handlers, logger.propagate, logger.level
    logger.handlers, logger.propagate = [], True
    yield
    logger.handlers, logger.propagate = handlers, propagate
    logger.setLevel(level)


async def _stored(dsn: str, tampering: str | None = None) -> tuple[repo.Row | None, list[repo.Row]]:
    """The chain's head and every audit row, oldest first, after ``tampering`` (as the database owner)."""
    pool = AsyncConnectionPool(dsn, open=False)
    await pool.open(wait=True)
    try:
        if tampering is not None:
            await _owner(pool, tampering)
        async with pool.connection() as conn:
            return await repo.audit_head(conn), list(reversed(await repo.pages(repo.list_audit, conn)))
    finally:
        await pool.close()


@pytest.mark.usefixtures("audit_logger")
def test_the_command_prints_the_head_or_the_first_break(pg_dsn: str, tmp_path: Path) -> None:
    (tmp_path / "dsn").write_text(pg_dsn + "\n")
    (tmp_path / "t.yaml").write_text(TARGETS)
    config = tmp_path / "ds.yaml"
    config.write_text(yaml.safe_dump({"database_dsn_ref": f"file:{tmp_path / 'dsn'}", "evidence_dir": "e",
                                      "workspace_dir": "w", "targets_file": "t.yaml"}))  # fmt: skip

    def ds(*args: Any) -> Any:
        return CliRunner().invoke(app, [str(a) for a in [*args, "--config", config]])

    assert ds("init").exit_code == 0
    first = ds("audit", "verify")  # its start writes keys.created rows, before it reads the chain
    head, rows = asyncio.run(_stored(pg_dsn))
    assert head is not None and len(rows) == head["seq"]
    assert (first.exit_code, first.stdout) == (
        0,
        f"chain: {head['seq']} row(s) verified\nhead: row {head['seq']} {head['hash']}\n",
    )
    (tmp_path / "siem.jsonl").write_text("\n".join(repo.siem_line(r) for r in rows) + "\n\n")
    against = ds("audit", "verify", "--siem", tmp_path / "siem.jsonl")
    assert (against.exit_code, against.stdout.splitlines()[1]) == (
        0,
        f"siem: {head['seq']} line(s) match: rows 1 to {head['seq']}",
    )
    (tmp_path / "bad.jsonl").write_text(repo.siem_line(rows[0]) + "\nnot json\n")
    bad = ds("audit", "verify", "--siem", tmp_path / "bad.jsonl")
    assert (bad.exit_code, bad.stderr) == (
        2,
        f"error: {tmp_path / 'bad.jsonl'} line 2 is not a deltasleuth.audit line\n",
    )
    asyncio.run(_stored(pg_dsn, "UPDATE ds.audit_log SET detail = '{\"x\": 1}' WHERE id = 2"))
    broken = ds("audit", "verify")
    row = rows[1]
    said = f"row {row['chain']['seq']} ({row['action']} {row['object']}) does not hash to its chain entry"
    assert (broken.exit_code, broken.stdout, broken.stderr) == (2, "", f"error: {said}: {ALTERED}\n")
    misplaced = CliRunner().invoke(app, ["audit", "--json", "verify"])
    assert (misplaced.exit_code, misplaced.stderr) == (2, "error: put the options after 'verify'\n")
