"""Adjudication in the store: a draw with a server salt, one score per draw that keeps
no note, the freeze while the cycle has a requested or approved sign-off, the attested adjudication (the
newest a person scored) and its scorer kept from approving, the trigger's rules, the purge's tombstone and
the P0 summary reading the attested adjudication."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import psycopg
import pytest

from deltasleuth import approvals
from deltasleuth.auth.principal import Principal
from deltasleuth.context import Context
from deltasleuth.core import jsonio
from deltasleuth.core.canonical.encoding import sha256_hex
from deltasleuth.reports.adjudicate import (
    SHEET_COLUMNS,
    AdjudicationConflict,
    AdjudicationError,
    UploadTooLarge,
    store_draw,
    store_score,
)
from deltasleuth.reports.csv import parse_csv, render_csv
from deltasleuth.reports.data import load_run
from deltasleuth.reports.summary import summary_document
from deltasleuth.runner import purge
from deltasleuth.store import repo
from tests.conftest import TESTER
from tests.unit.store.four_eyes import approve, approved, audit_as, person, request_signoff

from .conftest import doc, seed_run, unit_spec

pytestmark = pytest.mark.db
NOTE = "the customer phoned: 0712 345 678"  # a note that must never be stored
ADJUDICATION = "50 adjudicated keys agree with reviewers"


async def _cycle(ctx: Context) -> tuple[str, dict[str, Principal]]:
    """The ended first cycle of a live run of the unit spec over six keys (K002 differs, K003 timed out),
    run under a spec approval bob requested and carol and erin granted (the CLI stored the version: two
    approvers); the people by name, olga an operator."""
    samples = {f"K{i:03d}": (doc(f"K{i:03d}"), doc(f"K{i:03d}")) for i in range(1, 7)}
    samples["K002"] = (doc("K002", status="A"), doc("K002", status="B"))
    samples["K003"] = (doc("K003"), "timeout")
    run_id = await seed_run(ctx, samples, live=True)
    async with ctx.pool.connection() as conn:
        people = {name: await person(conn, name) for name in ("bob", "carol", "erin", "frank")}
        people["olga"] = await person(conn, "olga", "operator")
        run = await repo.get_run(conn, run_id)
        assert run is not None
        scopes = approvals.needs(unit_spec())
        approval_id = await approved(
            conn, run["spec_id"], people["bob"], people["carol"], people["erin"], scopes=scopes
        )
        await repo.set_run_approval(conn, run_id, approval_id)
        await repo.end_cycle(conn, run_id)
    return run_id, people


def reviewed(sheet: str, *, notes: str = NOTE, verdicts: dict[str, str] | None = None, **cells: str) -> bytes:
    """The drawn sheet filled in as a reviewer would: each engine verdict agreed (but ``verdicts``), a
    note on every row, and ``cells`` typed into the first row."""
    header, rows = parse_csv(sheet.encode())
    for row in rows:
        row["reviewer_verdict"] = (verdicts or {}).get(row["key"], row["engine_verdict"].lower())
        row["reviewer_notes"] = notes
    rows[0] |= cells
    return render_csv(header, rows).encode()


async def _audited(ctx: Context, action: str) -> list[tuple[str, str, dict[str, Any]]]:
    async with ctx.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT actor, object, detail FROM ds.audit_log WHERE action = %s ORDER BY id", (action,)
        )
        return await cur.fetchall()


async def test_a_draw_is_stored_with_a_server_salt_and_scored_once_without_notes(ctx: Context) -> None:
    run_id, people = await _cycle(ctx)
    olga, frank = people["olga"], people["frank"]
    drawn = await store_draw(ctx, run_id, 1, limit=4, who=olga)
    header, rows = parse_csv(drawn["sheet"].encode())
    keys = [row["key"] for row in rows]
    upload = reviewed(drawn["sheet"], verdicts={"K002": "match"}, top_findings="K999 was typed here")
    scored = await store_score(ctx, drawn["adjudication_id"], upload, who=frank)
    with pytest.raises(AdjudicationConflict) as again:
        await store_score(ctx, drawn["adjudication_id"], upload, who=frank)
    draws, scores = await _audited(ctx, "adjudication.draw"), await _audited(ctx, "adjudication.score")
    assert re.fullmatch(r"J-[0-9A-Z]{26}", drawn["adjudication_id"]) and re.fullmatch(
        "[0-9a-f]{32}", drawn["salt"]
    )
    assert (drawn["run_id"], drawn["cycle"], drawn["sample_limit"], drawn["drawn_by"]) == (
        run_id,
        1,
        4,
        olga.actor,
    )
    assert drawn["selection"] == {
        "candidates": 6,
        "keys": {row["key"]: row["selected_for"] for row in rows},
        "unrepresented": {"outcomes": [], "root_causes": []},
    }
    assert header == list(SHEET_COLUMNS) and len(rows) == 4 and {"K002", "K003"} <= set(keys)
    assert all(row["reviewer_verdict"] == row["reviewer_notes"] == "" for row in rows)
    assert draws == [
        (olga.actor, drawn["adjudication_id"], {"run_id": run_id, "cycle": 1, "salt": drawn["salt"]})
    ]
    # The sheet kept is the drawn one with the verdicts: no note, nothing else the reviewer typed.
    _, kept = parse_csv(scored["sheet"].encode())
    verdict = {row["key"]: row["engine_verdict"] for row in rows} | {"K002": "MATCH"}
    assert kept == [row | {"reviewer_verdict": verdict[row["key"]]} for row in rows]
    score = scored["score"]
    assert NOTE not in scored["sheet"] + json.dumps(score) and "K999" not in scored["sheet"]
    assert scored["upload_sha256"] == "sha256:" + hashlib.sha256(upload).hexdigest() == score["upload_sha256"]
    assert scored["sheet_sha256"] == hashlib.sha256(scored["sheet"].encode()).hexdigest()
    assert scored["score_sha256"] == sha256_hex(jsonio.dumps(score, sort_keys=True))
    assert (score["run_id"], score["cycle"], score["adjudication_id"], score["salt"]) == (
        run_id,
        1,
        drawn["adjudication_id"],
        drawn["salt"],
    )
    assert "sheet_sha256" not in score and (score["rows"], score["adjudicated"], score["agreed"]) == (4, 4, 3)
    assert score["disagreements"] == [
        {
            "key": "K002",
            "engine_outcome": "MISMATCH",
            "engine_verdict": "DIFFERENT",
            "reviewer_verdict": "MATCH",
        }
    ]
    assert (scored["passed"], scored["scored_by"]) == (False, frank.actor) and not score["exit"]["passed"]
    hashes = {name: scored[name] for name in ("upload_sha256", "sheet_sha256", "score_sha256")}
    assert scores == [(frank.actor, drawn["adjudication_id"], hashes | {"passed": False})]
    assert str(again.value) == f"adjudication {drawn['adjudication_id']} is already scored: draw a new sheet"


async def test_what_a_draw_or_score_refuses(ctx: Context) -> None:
    run_id, people = await _cycle(ctx)
    olga = people["olga"]
    drawn = await store_draw(ctx, run_id, 1, who=olga)

    async def refused(error: type[Exception], coroutine: Any) -> str:
        with pytest.raises(error) as caught:
            await coroutine
        return str(caught.value)

    foreign = render_csv(
        SHEET_COLUMNS, [{"key": "K404", "engine_outcome": "MATCH", "engine_verdict": "MATCH"}]
    )
    assert await refused(AdjudicationError, store_draw(ctx, run_id, 1, limit=501, who=olga)) == (
        "the adjudication limit must be 1 to 500"
    )
    assert await refused(LookupError, store_draw(ctx, "R-" + "0" * 26, 1, who=olga)) == (
        f"run R-{'0' * 26} not found"
    )
    assert await refused(AdjudicationConflict, store_draw(ctx, run_id, 2, who=olga)) == (
        f"cycle 2 of run {run_id} has not ended"
    )
    assert await refused(LookupError, store_score(ctx, "J-" + "0" * 26, b"key\r\n", who=olga)) == (
        f"adjudication J-{'0' * 26} not found"
    )
    assert await refused(
        UploadTooLarge, store_score(ctx, drawn["adjudication_id"], b" " * (2**20 + 1), who=olga)
    ) == ("the reviewed sheet is over 1 MiB")
    assert "row(s) for keys the run did not select" in await refused(
        AdjudicationError, store_score(ctx, drawn["adjudication_id"], foreign.encode(), who=olga)
    )
    assert await refused(AdjudicationError, store_score(ctx, drawn["adjudication_id"], b"", who=olga)) == (
        "the reviewed sheet cannot be read as a CSV sheet: the reviewed sheet is empty"
    )
    # A header naming a column twice would keep the last one's cells, not the ones reviewed: refused.
    twice = drawn["sheet"].replace("reviewer_notes\r\n", "reviewer_notes,reviewer_verdict\r\n", 1).encode()
    assert await refused(AdjudicationError, store_score(ctx, drawn["adjudication_id"], twice, who=olga)) == (
        "the reviewed sheet names column(s) reviewer_verdict more than once"
    )
    # A note longer than the CSV reader's own cell limit is read, and dropped as any note is; so are a
    # spreadsheet's unnamed columns beside the sheet's (two empty header cells name no column twice).
    header, rows = parse_csv(drawn["sheet"].encode())
    long = render_csv(header, [rows[0] | {"reviewer_notes": "x" * 140_000}, *rows[1:]]).encode()
    long = long.replace(b"\r\n", b",,\r\n")
    scored = await store_score(ctx, drawn["adjudication_id"], long, who=olga)
    assert scored["scored_by"] == olga.actor and "xxxx" not in scored["sheet"]


async def test_a_sign_off_freezes_the_cycle_and_attests_the_newest_adjudication_a_person_scored(
    ctx: Context,
) -> None:
    run_id, people = await _cycle(ctx)
    olga, frank = people["olga"], people["frank"]
    first = await store_draw(ctx, run_id, 1, who=olga)
    await store_score(ctx, first["adjudication_id"], reviewed(first["sheet"]), who=frank)
    by_the_cli = await store_draw(ctx, run_id, 1, who=olga)  # scored as the CLI would: no person
    await store_score(ctx, by_the_cli["adjudication_id"], reviewed(by_the_cli["sheet"]), who=TESTER)
    unscored = await store_draw(ctx, run_id, 1, who=olga)
    async with ctx.pool.connection() as conn:
        signoff = await request_signoff(conn, run_id, 1, olga, unit_spec())
        scored = await repo.get_adjudication(conn, first["adjudication_id"])
        request = await repo.get_approval(conn, signoff)
        run = await repo.get_run(conn, run_id)
        rebuilt = await approvals.rebuilt(conn, run, 1, unit_spec(), request["attests"])  # type: ignore[arg-type,index]
        authors = await repo.approval_authors(conn, signoff)
    assert scored is not None and request is not None and authors is not None
    attested = request["attests"]["adjudication"]
    assert attested == {
        "adjudication_id": first["adjudication_id"],
        "sheet_sha256": scored["sheet_sha256"],
        "score_sha256": scored["score_sha256"],
        "adjudicated": 6,
        "agreed": 6,
        "passed": False,  # 6 of 50
    }
    assert request["attests"]["format"] == 3 and rebuilt == request["attests"]
    frozen = f"cycle 1 of run {run_id} has a sign-off ({signoff}): revoke it before a new adjudication"
    with pytest.raises(AdjudicationConflict, match=re.escape(frozen)):
        await store_draw(ctx, run_id, 1, who=olga)
    with pytest.raises(AdjudicationConflict, match=re.escape(frozen)):
        await store_score(ctx, unscored["adjudication_id"], reviewed(unscored["sheet"]), who=frank)
    async with ctx.pool.connection() as conn:  # every writer meets the trigger
        with pytest.raises(psycopg.errors.CheckViolation, match=re.escape(f"has a sign-off ({signoff})")):
            await conn.execute(
                """INSERT INTO ds.adjudications (adjudication_id, run_id, cycle, salt, sample_limit,
                       selection, sheet, drawn_by)
                   VALUES (%s, %s, 1, %s, 1, '{}', '', 'user:U-0000000000000000')""",
                ("J-" + "1" * 26, run_id, "0" * 32),
            )
    # Frank scored the sheet it attests: he may not approve it, though others may.
    assert frank.actor in authors["persons"]
    async with ctx.pool.connection() as conn:
        with pytest.raises(repo.ApprovalRefused) as refused:
            await repo.decide_approval(conn, signoff, frank.actor, approve=True, first=True)
    async with ctx.pool.connection() as conn:
        assert (await approve(conn, signoff, people["carol"], people["erin"]))["status"] == "approved"
    with pytest.raises(AdjudicationConflict, match=re.escape(frozen)):
        await store_draw(ctx, run_id, 1, who=olga)
    async with ctx.pool.connection() as conn:
        await approvals.revoke(conn, audit_as(conn), signoff, "redo the sheet", who=people["carol"])
    assert (await store_draw(ctx, run_id, 1, who=olga))["sample_limit"] == 50  # thawed
    assert refused.value.check == "four_eyes"


async def test_the_trigger_allows_one_scoring_then_only_the_purge(ctx: Context) -> None:
    run_id, people = await _cycle(ctx)
    drawn = await store_draw(ctx, run_id, 1, who=people["olga"])
    scored = await store_score(ctx, drawn["adjudication_id"], reviewed(drawn["sheet"]), who=people["frank"])
    one = (drawn["adjudication_id"],)
    refusals = {
        "rescore": ("UPDATE ds.adjudications SET passed = true WHERE adjudication_id = %s", "scored once"),
        "edit": (
            "UPDATE ds.adjudications SET salt = repeat('1', 32) WHERE adjudication_id = %s",
            "scored once",
        ),
        "delete": ("DELETE FROM ds.adjudications WHERE adjudication_id = %s", "cannot be deleted"),
        "purge a hash": (
            """UPDATE ds.adjudications SET purged_at = now(), sheet = NULL, score_sha256 = repeat('0', 64),
                   selection = jsonb_set(selection, '{keys}', 'null'),
                   score = jsonb_set(score, '{disagreements}', 'null')
               WHERE adjudication_id = %s""",
            "keeps its hashes and counts",
        ),
        "truncate": ("TRUNCATE ds.adjudications CASCADE", "append-only"),  # ds.packs refers to it
    }
    for name, (statement, message) in refusals.items():
        async with ctx.pool.connection() as conn:
            with pytest.raises(psycopg.Error) as caught:
                await conn.execute(statement, None if name == "truncate" else one)  # type: ignore[arg-type]
            assert message in str(caught.value), name
    async with ctx.pool.connection() as conn:
        cur = await conn.execute("SELECT passed, salt FROM ds.adjudications WHERE adjudication_id = %s", one)
        assert await cur.fetchone() == (scored["passed"], scored["salt"])


async def test_a_purge_tombstones_the_cycle_s_adjudications(ctx: Context) -> None:
    run_id, people = await _cycle(ctx)
    drawn = await store_draw(ctx, run_id, 1, who=people["olga"])
    upload = reviewed(drawn["sheet"], verdicts={"K002": "match"})
    scored = await store_score(ctx, drawn["adjudication_id"], upload, who=people["frank"])
    unscored = await store_draw(ctx, run_id, 1, who=people["olga"])
    await purge.erase(ctx, TESTER, run_id, "test erasure", cycle=1)
    async with ctx.pool.connection() as conn:
        tombstones = await repo.list_adjudications(conn, run_id, 1)
    kept, blank = sorted(tombstones, key=lambda j: j["adjudication_id"])
    assert (kept["adjudication_id"], blank["adjudication_id"]) == (
        drawn["adjudication_id"],
        unscored["adjudication_id"],
    )
    assert all(
        j["purged_at"] is not None and j["sheet"] is None and j["selection"]["keys"] is None
        for j in tombstones
    )
    assert kept["selection"] == scored["selection"] | {"keys": None}
    assert kept["score"] == scored["score"] | {"disagreements": None}  # its counts stay
    names = ("upload_sha256", "sheet_sha256", "score_sha256", "passed", "scored_by", "scored_at", "salt")
    assert {name: kept[name] for name in names} == {name: scored[name] for name in names}
    assert blank["score"] is None and blank["score_sha256"] is None


async def test_the_p0_summary_reads_the_attested_adjudication_when_its_directory_has_none(
    ctx: Context, tmp_path: Path
) -> None:
    run_id, people = await _cycle(ctx)
    drawn = await store_draw(ctx, run_id, 1, who=people["olga"])
    await store_score(ctx, drawn["adjudication_id"], reviewed(drawn["sheet"]), who=people["frank"])

    async def check() -> tuple[str, str]:
        document = summary_document(await load_run(ctx, run_id, 1), tmp_path)
        (row,) = [row for row in document.sections[0].rows if row[0] == ADJUDICATION]
        return row[1], row[2]

    unsigned = await check()
    async with ctx.pool.connection() as conn:
        await request_signoff(conn, run_id, 1, people["olga"], unit_spec())
    assert unsigned[0] == "pending"  # no sign-off attests it yet
    assert await check() == (
        "fail",  # 6 keys, fewer than the exit's 50
        f"6 adjudicated, agreement 1.0000 (adjudication {drawn['adjudication_id']}, attested by the cycle's"
        " sign-off)",
    )
