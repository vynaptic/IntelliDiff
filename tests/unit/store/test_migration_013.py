"""Migration 013 over a populated 012 store: ds.labellings is added without
rewriting a table; a sign-off requested before it stays format 2, with the exclusion set it had; a
labelling starts unlabelled, is labelled once and then only purged, never deleted, and nothing is drawn or
labelled while its cycle has a requested or approved sign-off; ds.approval_authors keeps the scorer and
the closers and adds the labeller."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from deltasleuth import approvals
from deltasleuth.auth.principal import Principal
from deltasleuth.store import db, repo
from deltasleuth.store.db import migrate
from tests.unit.store.four_eyes import (
    SELECTION,
    counters,
    drawn_labelling,
    end,
    label,
    person,
    request_signoff,
    spec,
    start,
    upload,
)

pytestmark = pytest.mark.db

VERSION = spec("3.0.0")
ONCE = r"ds.labellings: labelling V-\w+ is labelled once, then only purged"


async def _all(conn: repo.Conn, sql: str, params: Any = ()) -> list[tuple]:
    return await (await conn.execute(sql, params)).fetchall()  # type: ignore[arg-type]


async def _cycle(conn: repo.Conn, olga: Principal, *, at: int = db.READINESS) -> str:
    """An ended first cycle of a run olga started under SSO: its id. On a store at migration ``at`` before
    014, its tombstone is written as ``repo.end_cycle`` wrote one then, without readiness."""
    run_id = await start(conn, await upload(conn, VERSION, olga), olga)
    if at >= db.READINESS:
        await end(conn, run_id, counters(1))
        return run_id
    await repo.set_counters(conn, run_id, counters(1))
    await conn.execute(
        """INSERT INTO ds.cycles (run_id, cycle, ended_at, counters, approval_id, keys_cut)
           SELECT run_id, cycle, now(), counters, approval_id, keys_cut FROM ds.runs WHERE run_id = %s""",
        (run_id,),
    )
    return run_id


async def _scored(conn: repo.Conn, run_id: str, by: Principal) -> str:
    """An adjudication of the cycle, drawn and scored by ``by``."""
    drawn = await repo.insert_adjudication(
        conn, run_id, 1, salt="0" * 32, limit=1, selection={"candidates": 1, "keys": {}}, sheet="key\r\n",
        drawn_by=by.actor,
    )  # fmt: skip
    score = {"adjudicated": 1, "agreed": 1, "disagreements": []}
    await repo.score_adjudication(
        conn, drawn["adjudication_id"], sheet="key\r\n", upload_sha256="sha256:" + "0" * 64,
        sheet_sha256="0" * 64, score=score, score_sha256="0" * 64, passed=False, scored_by=by.actor,
    )  # fmt: skip
    return drawn["adjudication_id"]


async def test_013_over_a_populated_012_store(empty_pool: AsyncConnectionPool) -> None:
    assert await migrate(empty_pool, upto=db.NOTIFY) == list(range(1, db.ACCURACY))
    tables = (
        "SELECT relname, relfilenode FROM pg_class WHERE relnamespace = 'ds'::regnamespace AND relkind = 'r'"
    )
    triggers = "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal ORDER BY tgname"
    async with empty_pool.connection() as conn:
        olga, sue = await person(conn, "olga", "operator"), await person(conn, "sue", "operator")
        run_id = await _cycle(conn, olga, at=db.NOTIFY)
        adjudication_id = await _scored(conn, run_id, sue)
        run = await repo.get_run(conn, run_id)
        scored = await repo.cycle_adjudication(conn, run_id, 1, adjudication_id)
        facts = await approvals._format_2(conn, run, 1, VERSION, scored)  # type: ignore[arg-type]
        signoff = await repo.create_approval(
            conn, "signoff", run["spec_id"], olga.actor, run_id=run_id, cycle=1, attests=facts,  # type: ignore[index]
            attests_sha256=approvals.attests_sha256(facts),
        )  # fmt: skip
        assert signoff is not None
        authors = await repo.approval_authors(conn, signoff["approval_id"])
        before, fired = dict(await _all(conn, tables)), await _all(conn, triggers)
    assert await migrate(empty_pool, upto=db.ACCURACY) == [db.ACCURACY]
    async with empty_pool.connection() as conn:
        after = dict(await _all(conn, tables))
        assert {name: after[name] for name in before} == before  # no rewrite
        assert set(after) - set(before) == {"labellings"}
        assert await _all(conn, triggers) == sorted(
            [*fired, ("labellings_lifecycle",), ("labellings_no_truncate",)]
        )
        audited = await _all(
            conn, "SELECT detail FROM ds.audit_log WHERE action = 'system.migrate' ORDER BY id"
        )
        # The sign-off requested before 013 stays format 2, with its hash and its exclusion set.
        stored = await repo.get_approval(conn, signoff["approval_id"])
        rebuilt = await approvals.rebuilt(conn, run, 1, VERSION, stored["attests"])  # type: ignore[arg-type,index]
        assert stored is not None and stored["attests"]["format"] == 2 and "accuracy" not in stored["attests"]
        assert rebuilt == stored["attests"] and approvals.attests_sha256(rebuilt) == stored["attests_sha256"]
        assert await repo.approval_authors(conn, signoff["approval_id"]) == authors
        assert set(authors["persons"]) == {olga.actor, sue.actor}  # type: ignore[index]
    assert audited[-1] == ({"versions": [db.ACCURACY]},)
    async with empty_pool.connection() as conn:  # its cycle is frozen for labellings too
        with pytest.raises(
            psycopg.errors.CheckViolation, match=rf"has a sign-off \({signoff['approval_id']}\)"
        ):
            await repo.insert_labelling(conn, drawn_labelling(run_id, olga))


async def test_a_labelling_is_labelled_once_then_only_purged(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        olga, frank = await person(conn, "olga", "operator"), await person(conn, "frank", "operator")
        run_id = await _cycle(conn, olga)
        drawn = await repo.insert_labelling(conn, drawn_labelling(run_id, olga))
        blank = await repo.insert_labelling(conn, drawn_labelling(run_id, olga))
    one = (drawn["labelling_id"],)
    assert drawn["labelling_id"].startswith("V-") and drawn["labelled_at"] is None and drawn["sheet"]
    check, restrict = psycopg.errors.CheckViolation, psycopg.errors.RestrictViolation
    unlabelled = [
        ({"labelling_id": "V-short"}, check, "labellings_labelling_id_check"),
        ({"salt": "0" * 32}, check, "labellings_salt_check"),  # 128 bits holding no letter
        ({"per_cause": 21}, check, "labellings_per_cause_check"),
        ({"accepted": 501}, check, "labellings_accepted_check"),
        ({"size": 0, "agreed": 0}, check, "labellings_check"),  # draws nothing
        ({"size": 600, "accepted": 300, "agreed": 200}, check, "labellings_check"),  # over 1,000 items
        ({"truth_source": "the core banking system"}, check, "labellings_truth_source_check"),
        ({"cycle": 2}, psycopg.errors.ForeignKeyViolation, "labellings_run_id_cycle_fkey"),  # not ended
        ({"labelled": True}, check, "must start unlabelled"),
    ]
    insert = """INSERT INTO ds.labellings (labelling_id, run_id, cycle, salt, size, per_cause, accepted,
                    agreed, truth_source, truth_as_of, selection, selection_sha256, sheet, drawn_by,
                    sheet_sha256, labels_sha256, accuracy, score_sha256, labelled_by, labelled_at)
                VALUES (%(labelling_id)s, %(run_id)s, %(cycle)s, %(salt)s, %(size)s, %(per_cause)s,
                    %(accepted)s, %(agreed)s, %(truth_source)s, %(truth_as_of)s, %(selection)s,
                    %(selection_sha256)s, %(sheet)s, %(drawn_by)s, %(hash)s, %(hash)s, %(score)s, %(hash)s,
                    %(by)s, %(at)s)"""
    for changes, error, message in unlabelled:
        labelled = changes.pop("labelled", False)
        values = drawn_labelling(run_id, olga) | {
            "labelling_id": "V-" + "0" * 26,
            "selection": Jsonb(SELECTION),
        }
        values |= {"hash": None, "score": None, "by": None, "at": None} | changes
        if labelled:
            values |= {"hash": "0" * 64, "score": Jsonb({}), "by": frank.actor, "at": "2026-10-01T00:00:00Z"}
        async with pool.connection() as conn:
            with pytest.raises(error, match=message):
                await conn.execute(insert, values)  # type: ignore[arg-type]
    fixed = [
        "UPDATE ds.labellings SET salt = repeat('ab', 16) WHERE labelling_id = %s",
        "UPDATE ds.labellings SET size = 2 WHERE labelling_id = %s",
        "UPDATE ds.labellings SET truth_source = 'OTHER' WHERE labelling_id = %s",
        "UPDATE ds.labellings SET sheet = 'edited' WHERE labelling_id = %s",  # not a labelling
        "UPDATE ds.labellings SET drawn_by = 'user:U-0000000000000000' WHERE labelling_id = %s",
        "UPDATE ds.labellings SET selection = '{}' WHERE labelling_id = %s",
    ]
    for statement in fixed:
        async with pool.connection() as conn:
            with pytest.raises(restrict, match=ONCE):
                await conn.execute(statement, one)  # type: ignore[arg-type]
    async with pool.connection() as conn:
        labelled = await label(conn, drawn["labelling_id"], frank)
        assert labelled is not None and labelled["labelled_by"] == frank.actor
        assert await label(conn, drawn["labelling_id"], frank) is None  # labelled already
    refused = [
        ("UPDATE ds.labellings SET labels_sha256 = repeat('4', 64) WHERE labelling_id = %s", ONCE),
        ("UPDATE ds.labellings SET labelled_at = now() WHERE labelling_id = %s", ONCE),  # a second label
        ("DELETE FROM ds.labellings WHERE labelling_id = %s", "cannot be deleted"),
        (
            """UPDATE ds.labellings SET purged_at = now(), sheet = NULL, score_sha256 = repeat('0', 64),
                   selection = jsonb_set(selection, '{items}', 'null') WHERE labelling_id = %s""",
            "keeps its hashes, counts and accuracy",
        ),
        (
            """UPDATE ds.labellings SET purged_at = now(), sheet = NULL,
                   selection = jsonb_set(selection, '{counts}', 'null') WHERE labelling_id = %s""",
            "keeps its hashes, counts and accuracy",
        ),
        (
            """UPDATE ds.labellings SET purged_at = now(), sheet = NULL, accuracy = '{}',
                   selection = jsonb_set(selection, '{items}', 'null') WHERE labelling_id = %s""",
            "keeps its hashes, counts and accuracy",
        ),
    ]
    for statement, message in refused:
        async with pool.connection() as conn:
            with pytest.raises(restrict, match=message):
                await conn.execute(statement, one)  # type: ignore[arg-type]
    async with pool.connection() as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="labellings is append-only"):
            await conn.execute("TRUNCATE ds.labellings")
    purge = """UPDATE ds.labellings SET purged_at = now(), sheet = NULL,
                   selection = jsonb_set(selection, '{items}', 'null') WHERE labelling_id = %s"""
    async with pool.connection() as conn:
        with pytest.raises(check, match="labellings_check1"):  # purged with its sheet
            await conn.execute(purge.replace(", sheet = NULL", ""), one)  # type: ignore[arg-type]
    async with pool.connection() as conn:
        for labelling_id in (drawn["labelling_id"], blank["labelling_id"]):
            assert (await conn.execute(purge, (labelling_id,))).rowcount == 1
        kept = await repo.get_labelling(conn, drawn["labelling_id"])
    assert kept is not None and kept["sheet"] is None and kept["selection"] == SELECTION | {"items": None}
    names = ("accuracy", "labels_sha256", "score_sha256", "labelled_by", "labelled_at", "salt")
    assert {name: kept[name] for name in names} == {name: labelled[name] for name in names}
    for statement in (
        "UPDATE ds.labellings SET purged_at = now() WHERE labelling_id = %s",
        "UPDATE ds.labellings SET sheet = 'back' WHERE labelling_id = %s",
    ):
        async with pool.connection() as conn:
            with pytest.raises(psycopg.Error, match=ONCE):
                await conn.execute(statement, one)  # type: ignore[arg-type]
    async with pool.connection() as conn:  # a tombstone is never labelled
        with pytest.raises(restrict, match=ONCE):
            await label(conn, blank["labelling_id"], frank)


async def test_a_sign_off_freezes_labellings_and_excludes_the_labeller(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        olga, frank = await person(conn, "olga", "operator"), await person(conn, "frank", "operator")
        sue, fay, gus = [await person(conn, name, "operator") for name in ("sue", "fay", "gus")]
        run_id = await _cycle(conn, olga)
        await _scored(conn, run_id, sue)
        labelled = await repo.insert_labelling(conn, drawn_labelling(run_id, olga))
        blank = await repo.insert_labelling(conn, drawn_labelling(run_id, olga))
        assert await label(conn, labelled["labelling_id"], frank) is not None
        signoff = await request_signoff(conn, run_id, 1, olga, VERSION)
        attests = (await repo.get_approval(conn, signoff))["attests"]  # type: ignore[index]
        ledger = {"sample": {"ledger": [{"closed_by": fay.actor}, {"closed_by": gus.actor}]}}
        rows = await _all(
            conn,
            """SELECT (ds.approval_authors(jsonb_populate_record(NULL::ds.approvals,
                          to_jsonb(a) || jsonb_build_object('attests', a.attests || e.extra)))).persons
               FROM ds.approvals a, (VALUES ('{}'::jsonb), (%s), ('{"accuracy": null}')) AS e (extra)
               WHERE a.approval_id = %s""",
            (Jsonb(ledger), signoff),
        )
    assert (attests["format"], attests["accuracy"]["labelling_id"]) == (3, labelled["labelling_id"])
    asked, sampled, unlabelled = (set(persons) for (persons,) in rows)
    assert asked == {
        olga.actor,
        sue.actor,
        frank.actor,
    }  # the requester and starter, the scorer, the labeller
    assert sampled == asked | {fay.actor, gus.actor}  # the closers stay
    assert unlabelled == {olga.actor, sue.actor}  # a sign-off attesting no labelling excludes no labeller
    frozen = rf"ds.labellings: cycle 1 of run {run_id} has a sign-off \({signoff}\)"
    async with pool.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation, match=frozen):
            await repo.insert_labelling(conn, drawn_labelling(run_id, olga))
    async with pool.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation, match=frozen):
            await label(conn, blank["labelling_id"], frank)
    async with pool.connection() as conn:  # the labeller may not approve it, though another may
        with pytest.raises(repo.ApprovalRefused) as refused:
            await repo.decide_approval(conn, signoff, frank.actor, approve=True, first=True)
    assert refused.value.check == "approver"  # an operator; the four-eyes refusal is test_approvals'
