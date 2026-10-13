"""Migration 011 (docs/mvp/SAMPLING.md §12): the run_keys labels, added without a rewrite or a scan and
checked on new rows; a run's pending draw and each cycle's judgement; ds.sample_draws, made as of now()
for a running run's current cycle before its keys and only ever closed, once; the sampling scope; closers
excluded from a sign-off; and the repo functions storage needs."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from deltasleuth.connectors.base import utc_iso
from deltasleuth.core import sampling
from deltasleuth.runner import census
from deltasleuth.store import db, repo
from deltasleuth.store.db import MIGRATIONS, migrate
from tests.conftest import TEST_DATA_KEY, TEST_SEALER, TESTER
from tests.unit.store.four_eyes import approved, end, person, request_signoff, spec, start, upload

pytestmark = pytest.mark.db

KEYS = 1_000_000  # run_keys rows the 010 store holds
NOT_VALID = [
    "run_keys_retries", "run_keys_signoff_member_needs_stratum", "run_keys_signoff_stratum_format",
    "run_keys_stratum_format",
]  # fmt: skip
MADE = (
    "ds.sample_draws: draw D-\\w+ is made as of now\\(\\), for a running run's current cycle, before its keys"
)
CLOSED_ONCE = "ds.sample_draws: draw D-\\w+ is only ever closed, once, as of now\\(\\), after its cycle ended"
POPULATION = ["listing:new-api:GET /v1/customers", "table:legacy-db/customers"]


async def _all(conn: repo.Conn, sql: str, params: Any = ()) -> list[tuple]:
    return await (await conn.execute(sql, params)).fetchall()  # type: ignore[arg-type]


async def test_011_over_a_populated_010_store(empty_pool: AsyncConnectionPool) -> None:
    """No rewrite or scan of run_keys: its file stays, its checks are NOT VALID and its defaults are the
    catalogue's; earlier rows read NULL or the default, new rows are checked; approvals of every scope
    pass the rebuilt check, which also takes sampling."""
    assert await migrate(empty_pool, upto=db.SAMPLING - 1) == list(range(1, 11))
    async with empty_pool.connection() as conn:
        amy, bob = await person(conn, "amy", "spec_author"), await person(conn, "bob")
        spec_id = await upload(conn, spec(), amy)
        scopes = ["clear", "unlisted_clear", "known_diffs", "population_filters"]
        approval_id = await approved(conn, spec_id, amy, bob, scopes=scopes)
        await conn.execute(
            """INSERT INTO ds.runs (run_id, spec_id, mode, status, retention, started_at)
               VALUES ('R-' || repeat('A', 26), %s, 'batch', 'running', 'standard', now())""",
            (spec_id,),
        )
        await conn.execute(
            """INSERT INTO ds.run_keys (run_id, cycle, key_masked, key_enc, seq)
               SELECT 'R-' || repeat('A', 26), 1, 'm' || g, decode(repeat('01', 30), 'hex'), g
               FROM generate_series(0, %s - 1) g""",
            (KEYS,),
        )
        [(before,)] = await _all(conn, "SELECT pg_relation_filenode('ds.run_keys')")
    assert await migrate(empty_pool, upto=db.SAMPLING) == [db.SAMPLING]
    async with empty_pool.connection() as conn:
        [(after,)] = await _all(conn, "SELECT pg_relation_filenode('ds.run_keys')")
        checks = await _all(
            conn,
            """SELECT conname, convalidated FROM pg_constraint WHERE conrelid = 'ds.run_keys'::regclass
                 AND conname LIKE 'run_keys\\_%%' AND contype = 'c' AND conname = ANY(%s) ORDER BY conname""",
            (NOT_VALID,),
        )
        defaults = await _all(
            conn,
            """SELECT attname, atthasmissing FROM pg_attribute WHERE attrelid = 'ds.run_keys'::regclass
                 AND attname IN ('signoff_member', 'retries') ORDER BY attname""",
        )
        earlier = await _all(
            conn,
            """SELECT count(*), count(stratum), count(signoff_stratum), bool_or(signoff_member), max(retries)
               FROM ds.run_keys""",
        )
        audited = await _all(
            conn, "SELECT detail FROM ds.audit_log WHERE action = 'system.migrate' ORDER BY id"
        )
        [(kept,)] = await _all(conn, "SELECT scopes FROM ds.approvals WHERE approval_id = %s", (approval_id,))
    assert after == before  # no rewrite
    assert checks == [(name, False) for name in NOT_VALID]  # nothing validated earlier rows
    assert defaults == [("retries", True), ("signoff_member", True)]  # the catalogue holds the defaults
    assert earlier == [(KEYS, 0, 0, False, 0)]
    assert audited[-1] == ({"versions": [db.SAMPLING]},) and kept == scopes
    refused = [
        ("stratum", "-leading", "run_keys_stratum_format"),
        ("stratum", "degree=d0 flagged=N", "run_keys_stratum_format"),  # a space
        ("signoff_stratum", "a" * 257, "run_keys_signoff_stratum_format"),
        ("retries", 11, "run_keys_retries"),
        ("signoff_member", True, "run_keys_signoff_member_needs_stratum"),
    ]
    for column, value, constraint in refused:
        async with empty_pool.connection() as conn:
            with pytest.raises(psycopg.errors.CheckViolation, match=constraint):
                await _new_key(conn, {column: value})
    async with empty_pool.connection() as conn:
        await _new_key(
            conn, {"stratum": "degree=d0|flagged=N", "signoff_stratum": "degree=d0", "signoff_member": True}
        )
        carol = await person(conn, "carol", "spec_author")
        sampled = await upload(conn, spec("1.1.0"), carol)
        assert await repo.create_approval(conn, "spec", sampled, carol.actor, scopes=["sampling"])
    async with empty_pool.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation, match="approvals_scopes_check"):
            await repo.create_approval(conn, "spec", spec_id, carol.actor, scopes=["bogus"])


async def _new_key(conn: repo.Conn, labels: dict[str, Any]) -> None:
    """A new key of the populated store's run, with ``labels`` (stratum, signoff_stratum, signoff_member,
    retries) over their defaults."""
    values = {"stratum": None, "signoff_stratum": None, "signoff_member": False, "retries": 0} | labels
    await conn.execute(
        """INSERT INTO ds.run_keys (run_id, cycle, key_masked, key_enc, seq, stratum, signoff_stratum,
               signoff_member, retries)
           VALUES ('R-' || repeat('A', 26), 1, 'new', decode(repeat('01', 30), 'hex'), %s, %s, %s, %s, %s)""",
        (KEYS, *values.values()),
    )


async def _sampled_run(
    conn: repo.Conn, *, status: str = "running", due: timedelta | None = timedelta(0)
) -> tuple[str, int]:
    """A bottom_k run with no keys, ``status``, and a draw pending from ``due`` after now (None: none)."""
    dan = await person(conn, "dan", "operator")
    spec_id = await upload(conn, spec(), dan)
    run_id = repo.new_run_id()
    params = {"keys_source": "hash_sample", "method": "bottom_k", "population": POPULATION}
    await repo.create_run(
        conn, spec_id, "batch", {}, params, run_id=run_id, created_by=dan.actor, retention="standard",
        data_key=TEST_DATA_KEY,
    )  # fmt: skip
    await repo.set_run_status(conn, run_id, status)
    if due is not None:
        await repo.set_draw(conn, run_id, {"due": utc_iso(await _now(conn) + due)})
    return run_id, spec_id


def _draw(run_id: str, spec_id: int, **changes: Any) -> dict[str, Any]:
    return {
        "draw_id": repo.new_draw_id(), "run_id": run_id, "spec_id": spec_id, "cycle": 1, "method": "bottom_k",
        "population": POPULATION, "salt_label": "cutover-q4", "nonce": sampling.nonce(), "keys": 3,
        "frame": {"keys": 12, "origin": {"a": 2, "b": 1, "both": 9}, "unfit": 0},
        "strata": [{"label": "degree=d0", "drawn": 2}, {"label": "flagged=Y", "drawn": 1}],
        "selection_sha256": "0" * 64,
    } | changes  # fmt: skip


SEALED = TEST_SEALER.seal_keys({"k1": "m-k1", "k2": "m-k2", "k3": "m-k3"})
LABELS = [("degree=d0|flagged=N", "degree=d0", True), ("degree=d0|flagged=N", "degree=d0", True),
          ("degree=d1|flagged=Y", "flagged=Y", True)]  # fmt: skip


async def test_a_draw_is_made_as_of_now_for_a_running_run_s_current_cycle_before_its_keys(
    pool: AsyncConnectionPool,
) -> None:
    async with pool.connection() as conn:
        run_id, spec_id = await _sampled_run(conn)
        row = await repo.insert_draw(conn, _draw(run_id, spec_id), SEALED, LABELS)
        [(now,)] = await _all(conn, "SELECT now()")
        lines = [line async for line in repo.selection_lines(conn, run_id, 1)]
    assert (row["drawn_at"], row["population"], row["closed_at"]) == (now, POPULATION, None)
    assert lines == [(seq, f"m-k{seq + 1}", *LABELS[seq]) for seq in range(3)]
    text = "".join(f"{seq}\tm-k{seq + 1}\t{s}\t{o}\ttrue\n" for seq, (s, o, _) in enumerate(LABELS))
    assert sampling.selection_sha256(lines) == hashlib.sha256(text.encode()).hexdigest()
    check = psycopg.errors.CheckViolation
    async with pool.connection() as conn:
        paused, paused_spec = await _sampled_run(conn, status="paused")
        idle, idle_spec = await _sampled_run(conn, due=None)
        keyed, keyed_spec = await _sampled_run(conn)
        await conn.execute(
            """INSERT INTO ds.run_keys (run_id, cycle, key_masked, key_enc, seq)
               VALUES (%s, 1, 'early', decode(repeat('01', 30), 'hex'), 0)""",
            (keyed,),
        )
    refused = [
        _draw(paused, paused_spec),  # not running
        _draw(idle, idle_spec),  # no draw pending
        _draw(run_id, spec_id, cycle=2),  # not its current cycle
        _draw(keyed, keyed_spec),  # after its keys
    ]
    for draw in refused:
        async with pool.connection() as conn:
            with pytest.raises(check, match=MADE):
                await repo.insert_draw(conn, draw, SEALED, LABELS)
    async with pool.connection() as conn:
        again, again_spec = await _sampled_run(conn)
        values = _draw(again, again_spec) | {"frame": Jsonb({}), "strata": Jsonb([])}
        with pytest.raises(check, match=MADE):
            await conn.execute(
                """INSERT INTO ds.sample_draws (draw_id, run_id, spec_id, cycle, method, population,
                       salt_label, nonce, drawn_at, keys, frame, strata, selection_sha256)
                   VALUES (%(draw_id)s, %(run_id)s, %(spec_id)s, 1, 'bottom_k', %(population)s,
                       %(salt_label)s, %(nonce)s, now() - interval '1 day', 3, %(frame)s, %(strata)s,
                       %(selection_sha256)s)""",
                values,
            )
    async with pool.connection() as conn:
        with pytest.raises(check, match=MADE):  # one draw a cycle: its keys exist
            await repo.insert_draw(conn, _draw(run_id, spec_id), SEALED, LABELS)


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"draw_id": "D-short"}, psycopg.errors.CheckViolation),
        ({"nonce": "0" * 32}, psycopg.errors.CheckViolation),  # 128 bits holding no letter
        ({"nonce": "A" * 32}, psycopg.errors.CheckViolation),
        ({"salt_label": "no spaces"}, psycopg.errors.CheckViolation),
        ({"population": []}, psycopg.errors.CheckViolation),
        ({"population": ["a", "b", "c"]}, psycopg.errors.CheckViolation),
        ({"keys": 0}, psycopg.errors.CheckViolation),
        ({"method": "systematic"}, psycopg.errors.CheckViolation),
        ({"selection_sha256": "x" * 64}, psycopg.errors.CheckViolation),
        ({"spec_id": 0}, psycopg.errors.ForeignKeyViolation),  # not the run's spec
    ],
)
async def test_a_draw_s_columns_are_checked(pool: AsyncConnectionPool, changes: dict, error: type) -> None:
    async with pool.connection() as conn:
        run_id, spec_id = await _sampled_run(conn)
    async with pool.connection() as conn:
        with pytest.raises(error):
            await repo.insert_draw(conn, _draw(run_id, spec_id) | changes, SEALED, LABELS)


async def test_a_draw_is_only_ever_closed_once_as_of_now_after_its_cycle_ended(
    pool: AsyncConnectionPool,
) -> None:
    async with pool.connection() as conn:
        fay = await person(conn, "fay", "operator")
        run_id, spec_id = await _sampled_run(conn)
        draw = await repo.insert_draw(conn, _draw(run_id, spec_id), SEALED, LABELS)
    restrict = psycopg.errors.RestrictViolation
    async with pool.connection() as conn:
        with pytest.raises(restrict, match=CLOSED_ONCE):  # its cycle is live
            await repo.close_draws(conn, [draw["draw_id"]], fay.actor, "fixed the degree mapping")
    judgement = {"status": "failed", "strata": [{"label": "degree=d0", "verdict": "failed"}]}
    async with pool.connection() as conn:
        await repo.end_cycle(conn, run_id, sample=judgement)
        cycle = await repo.get_cycle(conn, run_id, 1)
    assert cycle is not None and cycle["sample"] == judgement
    refused = [
        ("UPDATE ds.sample_draws SET keys = keys + 1 WHERE draw_id = %s", restrict),
        ("UPDATE ds.sample_draws SET closed_by = %s, closed_at = now() - interval '1 day', close_note = 'x'"
         " WHERE draw_id = %s", restrict),  # backdated
        ("UPDATE ds.sample_draws SET closed_by = %s, closed_at = now() WHERE draw_id = %s",
         psycopg.errors.CheckViolation),  # without its note
        ("UPDATE ds.sample_draws SET closed_by = 'local:ops@host', closed_at = now(), close_note = 'x'"
         " WHERE draw_id = %s", psycopg.errors.CheckViolation),  # closed by a person only
        ("DELETE FROM ds.sample_draws WHERE draw_id = %s", restrict),
    ]  # fmt: skip
    for statement, error in refused:
        params = (fay.actor, draw["draw_id"]) if statement.count("%s") == 2 else (draw["draw_id"],)
        async with pool.connection() as conn:
            with pytest.raises(error):
                await conn.execute(statement, params)  # type: ignore[arg-type]
    async with pool.connection() as conn:
        [closed] = await repo.close_draws(conn, [draw["draw_id"]], fay.actor, "fixed the degree mapping")
        [(now,)] = await _all(conn, "SELECT now()")
        assert await repo.close_draws(conn, [draw["draw_id"]], fay.actor, "again") == []  # closed already
    assert (closed["closed_by"], closed["closed_at"], closed["close_note"]) == (
        fay.actor, now, "fixed the degree mapping"
    )  # fmt: skip
    for statement in (
        "UPDATE ds.sample_draws SET closed_at = now(), close_note = 'again' WHERE draw_id = %s",
        "DELETE FROM ds.sample_draws WHERE draw_id = %s",
    ):
        async with pool.connection() as conn:
            with pytest.raises(restrict, match=CLOSED_ONCE):
                await conn.execute(statement, (draw["draw_id"],))  # type: ignore[arg-type]
    async with pool.connection() as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="sample_draws is append-only"):
            await conn.execute("TRUNCATE ds.sample_draws")


async def test_the_repo_functions_storage_needs(pool: AsyncConnectionPool) -> None:
    """Due draws, live sampled runs by population, a next cycle's copied labels with retries reset, and no
    key census for a hash sample."""
    async with pool.connection() as conn:
        due, due_spec = await _sampled_run(conn)
        later, _ = await _sampled_run(conn, due=timedelta(hours=1))
        paused, _ = await _sampled_run(conn, status="paused")
        cancelled, _ = await _sampled_run(conn, status="cancelled")
        assert set(await repo.draws_due(conn)) & {due, later, paused, cancelled} == {due}
        live = await repo.live_sampled_runs(conn, ["table:legacy-db/customers", "table:x/y"], lock=True)
        assert {row["run_id"] for row in live} >= {due, later, paused} and cancelled not in {
            row["run_id"] for row in live
        }
        assert await repo.live_sampled_runs(conn, ["table:legacy-db/accounts"]) == []
        await repo.insert_draw(conn, _draw(due, due_spec), SEALED, LABELS)
        assert await repo.set_draw(conn, due, None) and due not in await repo.draws_due(conn)
        await conn.execute("UPDATE ds.run_keys SET retries = 2 WHERE run_id = %s", (due,))
        await end(conn, due)
        assert await repo.start_next_cycle(conn, due, await _now(conn)) == 2
        copied = await _all(
            conn,
            """SELECT stratum, signoff_stratum, signoff_member, retries FROM ds.run_keys
               WHERE run_id = %s AND cycle = 2 ORDER BY seq""",
            (due,),
        )
        tables = {"tables": {"a": {"target": "legacy-db", "name": "customers"}}}
        sampled, _ = await _sampled_run(conn)
        keyed = await start(conn, due_spec, TESTER, params={"keys_source": "explicit"} | tables)
        await conn.execute(
            "UPDATE ds.runs SET params = params || %s WHERE run_id = %s", (Jsonb(tables), sampled)
        )
        pending = set(await repo.census_due(conn))
        runs = [await repo.get_run(conn, run_id) for run_id in (keyed, sampled)]
    assert copied == [(*labels, 0) for labels in LABELS]
    assert keyed in pending and sampled not in pending  # a frame is its own census
    assert [census.pending(run) for run in runs] == [["a"], []]  # type: ignore[arg-type]


async def _now(conn: repo.Conn) -> Any:
    [(now,)] = await _all(conn, "SELECT now()")
    return now


async def test_a_sign_off_excludes_whoever_closed_a_draw_of_its_ledger(pool: AsyncConnectionPool) -> None:
    """011's ds.approval_authors adds the closers of the ledger a sign-off attests (§10); the scorer of its
    adjudication stays excluded (tests/unit/reports/test_adjudications.py)."""
    async with pool.connection() as conn:
        amy, olga = await person(conn, "amy", "spec_author"), await person(conn, "olga", "operator")
        fay, gus = await person(conn, "fay", "operator"), await person(conn, "gus", "operator")
        version = spec("2.0.0")
        spec_id = await upload(conn, version, amy)
        run_id = await start(conn, spec_id, olga)
        await end(conn, run_id, {"cycle": 1})
        signoff = await request_signoff(conn, run_id, 1, olga, version)
        ledger = {
            "sample": {"ledger": [{"closed_by": fay.actor}, {"closed_by": None}, {"closed_by": gus.actor}]}
        }
        rows = await _all(
            conn,
            """SELECT (ds.approval_authors(jsonb_populate_record(NULL::ds.approvals,
                          to_jsonb(a) || jsonb_build_object('attests', a.attests || e.extra)))).persons
               FROM ds.approvals a, (VALUES ('{}'::jsonb), ('{"sample": null}'), (%s)) AS e (extra)
               WHERE a.approval_id = %s""",
            (Jsonb(ledger), signoff),
        )
    unrelated, census, sampled = (set(persons) for (persons,) in rows)
    assert unrelated == census == {olga.actor}  # the requester, who also started the run
    assert sampled == {olga.actor, fay.actor, gus.actor}


async def test_store_at_011_opens_with_001_to_010_files(
    pool: AsyncConnectionPool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release before 011 beside the new one: its migrate applies nothing."""
    for path in MIGRATIONS.glob("0[01][0-9]_*.sql"):
        if int(path.name[:3]) < db.SAMPLING:
            (tmp_path / path.name).write_bytes(path.read_bytes())
    monkeypatch.setattr(db, "MIGRATIONS", tmp_path)
    assert await migrate(pool) == []
    async with pool.connection() as conn:
        assert await _all(conn, "SELECT max(version) FROM ds.schema_version") == [(17,)]
