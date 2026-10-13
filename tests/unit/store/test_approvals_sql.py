"""The four-eyes rule in SQL (docs/mvp/APPROVALS.md §3, §7): who is accountable for what, the lineage
of a spec's versions, and every refusal of ds.tg_approvals, which checks every writer."""

from __future__ import annotations

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from deltasleuth.store import repo
from tests.conftest import TESTER
from tests.unit.store.four_eyes import (
    approve,
    approved,
    counters,
    end,
    person,
    request,
    request_signoff,
    spec,
    start,
    token,
    upload,
)

pytestmark = pytest.mark.db

V1, V2, V3 = spec("1.0.0"), spec("1.1.0"), spec("1.2.0")


async def _authors(conn: repo.Conn, approval_id: str) -> tuple[list[str], bool]:
    row = await repo.approval_authors(conn, approval_id)
    assert row is not None
    return row["persons"], row["unattributed"]


async def _spec_accountable(conn: repo.Conn, spec_id: int) -> tuple[list[str], bool]:
    cur = await conn.execute("SELECT persons, unattributed FROM ds.spec_accountable(%s)", (spec_id,))
    return await cur.fetchone()  # type: ignore[return-value]


async def test_accountable_and_attributed_per_actor(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        fay, carol = await person(conn, "fay", "admin"), await person(conn, "carol", "spec_author")
        owned = await token(conn, "ci-bot", fay, carol)  # created by a person, owned by another
        unowned = await token(conn, "ops-bot", fay)
        by_cli = await token(conn, "cli-bot", TESTER)  # created with 'ds token create', no owner
        actors = [fay.actor, owned.actor, unowned.actor, by_cli.actor, "token:T-" + "0" * 16, TESTER.actor,
                  "dev:loopback", "system:worker", "api", "ds", None]  # fmt: skip
        cur = await conn.execute(
            "SELECT ds.accountable(a), ds.attributed(a) FROM unnest(%s::text[]) WITH ORDINALITY AS u (a, n)"
            " ORDER BY n",
            (actors,),
        )
        rows = await cur.fetchall()
    assert rows == [
        ([fay.actor], True),  # a person is accountable for itself
        ([fay.actor, carol.actor], False),  # a token shows who holds it, not who wrote the content
        ([fay.actor], False),
        ([], False),  # created on the CLI, owned by no one
        ([], False),  # an unknown token
        *[([], False)] * 6,  # the CLI, developer mode, DeltaSleuth, rows before 005, none
    ]


async def test_the_lineage_runs_from_the_newest_approved_version(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        bob, carol, dave, erin = [await person(conn, n) for n in ("bob", "carol", "dave", "erin")]
        v1 = await upload(conn, V1, bob)
        v2 = await upload(conn, V2, carol)  # a colleague's bump of Bob's unapproved rules
        assert await _spec_accountable(conn, v1) == ([bob.actor], False)
        assert await _spec_accountable(conn, v2) == (sorted([bob.actor, carol.actor]), False)
        await approved(conn, v2, carol, dave)
        v3 = await upload(conn, V3, bob)  # the approval resets the lineage
        assert await _spec_accountable(conn, v3) == ([bob.actor], False)
        assert await upload(conn, V3, erin) == v3  # the same content again: another spec.create row
        assert await _spec_accountable(conn, v3) == (sorted([bob.actor, erin.actor]), False)
        a3 = await request(conn, v3, bob)
        assert await _authors(conn, a3) == (sorted([bob.actor, erin.actor]), False)
        row = await approve(conn, a3, carol)  # two people taking turns approve each other
    assert (row["status"], row["authors"], row["unattributed"]) == (
        "approved", sorted([bob.actor, erin.actor]), False
    )  # fmt: skip


@pytest.mark.parametrize("creator", [TESTER.actor, None], ids=["cli", "before_005"])
async def test_an_unattributed_upload_in_the_lineage_needs_two_approvers(
    pool: AsyncConnectionPool, creator: str | None
) -> None:
    async with pool.connection() as conn:
        bob, carol, dave = [await person(conn, n) for n in ("bob", "carol", "dave")]
        await upload(conn, V1, creator)
        v2 = await upload(conn, V2, bob)
        approval_id = await request(conn, v2, bob)
        assert await _authors(conn, approval_id) == ([bob.actor], True)
    async with pool.connection() as conn:
        with pytest.raises(repo.ApprovalRefused, match="needs 2 approver") as refused:
            await repo.decide_approval(conn, approval_id, carol.actor, approve=True)
        assert refused.value.check == "approvers"
    async with pool.connection() as conn:
        row = await approve(conn, approval_id, carol, dave)
    assert (row["first_by"], row["decided_by"], row["unattributed"]) == (carol.actor, dave.actor, True)


async def test_a_token_upload_needs_two_approvers_outside_its_creator_and_owner(
    pool: AsyncConnectionPool,
) -> None:
    """Bob uploads through a token Fay created and Carol owns: the token hides Bob, so Bob alone is
    refused, and Bob then Dave approve."""
    async with pool.connection() as conn:
        fay, carol, bob, dave = [await person(conn, n) for n in ("fay", "carol", "bob", "dave")]
        ci = await token(conn, "ci-bot", fay, carol)
        spec_id = await upload(conn, V1, ci)
        approval_id = await request(conn, spec_id, ci)
        assert await _authors(conn, approval_id) == (sorted([fay.actor, carol.actor]), True)
    for insider in (fay, carol):
        async with pool.connection() as conn:
            with pytest.raises(repo.ApprovalRefused, match=r"\(four-eyes\)") as refused:
                await repo.decide_approval(conn, approval_id, insider.actor, approve=True, first=True)
            assert refused.value.check == "four_eyes"
    async with pool.connection() as conn:
        with pytest.raises(repo.ApprovalRefused, match="needs 2 approver"):
            await repo.decide_approval(conn, approval_id, bob.actor, approve=True)
    async with pool.connection() as conn:
        assert await repo.decide_approval(conn, approval_id, bob.actor, approve=True, first=True)
    async with pool.connection() as conn:
        with pytest.raises(repo.ApprovalRefused, match="needs 2 approver"):  # not twice
            await repo.decide_approval(conn, approval_id, bob.actor, approve=True)
    async with pool.connection() as conn:
        row = await repo.decide_approval(conn, approval_id, dave.actor, approve=True)
    assert row is not None
    assert (row["status"], row["first_by"], row["decided_by"], row["authors"]) == (
        "approved", bob.actor, dave.actor, sorted([fay.actor, carol.actor])
    )  # fmt: skip


async def test_an_attributed_request_takes_one_approver_in_one_step(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        bob, carol = await person(conn, "bob"), await person(conn, "carol")
        approval_id = await request(conn, await upload(conn, V1, bob), bob)
    async with pool.connection() as conn:
        with pytest.raises(repo.ApprovalRefused, match="needs 1 approver"):
            await repo.decide_approval(conn, approval_id, carol.actor, approve=True, first=True)
    async with pool.connection() as conn:  # the requester and uploader
        with pytest.raises(repo.ApprovalRefused, match=r"may not approve .* \(four-eyes\)"):
            await repo.decide_approval(conn, approval_id, bob.actor, approve=True)
    async with pool.connection() as conn:
        row = await approve(conn, approval_id, carol)
    assert (row["first_by"], row["decided_by"], row["authors"]) == (None, carol.actor, [bob.actor])


async def test_the_decider_must_be_an_enabled_rule_approver(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        bob = await person(conn, "bob")
        author = await person(conn, "ann", "spec_author")
        gone = await person(conn, "gus")
        await repo.set_user_disabled(conn, gone.actor.removeprefix("user:"), TESTER.actor)
        approval_id = await request(conn, await upload(conn, V1, bob), bob)
    for decider in (author.actor, gone.actor, "user:U-" + "0" * 16):
        for approving in (True, False):  # a rejection too
            async with pool.connection() as conn:
                with pytest.raises(repo.ApprovalRefused, match="is not an enabled rule_approver") as refused:
                    await repo.decide_approval(conn, approval_id, decider, approve=approving, note="no")
                assert refused.value.check == "approver"


async def test_the_first_approver_may_reject(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        bob, carol = await person(conn, "bob"), await person(conn, "carol")
        approval_id = await request(conn, await upload(conn, V1, TESTER.actor), bob)
        await repo.decide_approval(conn, approval_id, carol.actor, approve=True, first=True)
        row = await repo.decide_approval(
            conn, approval_id, carol.actor, approve=False, note="wrong clear path"
        )
        assert await repo.decide_approval(conn, approval_id, bob.actor, approve=True) is None  # decided
    assert row is not None
    assert (row["status"], row["first_by"], row["decided_by"], row["decision_note"]) == (
        "rejected", carol.actor, carol.actor, "wrong clear path"
    )  # fmt: skip


async def test_a_signoff_excludes_the_spec_s_authors_the_run_s_creator_and_its_requester(
    pool: AsyncConnectionPool,
) -> None:
    async with pool.connection() as conn:
        bob, carol, dave, olga = [await person(conn, n) for n in ("bob", "carol", "dave", "olga")]
        ci = await token(conn, "ci-runner", olga, olga)
        spec_id = await upload(conn, V1, bob)
        spec_approval = await approved(conn, spec_id, bob, carol)
        by_person = await start(conn, spec_id, dave, approval_id=spec_approval)
        by_token = await start(conn, spec_id, ci, approval_id=spec_approval)
        for run_id in (by_person, by_token):
            await end(conn, run_id, counters(1))
        signoff = await request_signoff(conn, by_person, 1, olga, V1)
        token_signoff = await request_signoff(conn, by_token, 1, carol, V1)
        assert await _authors(conn, signoff) == (sorted([bob.actor, dave.actor, olga.actor]), False)
        assert await _authors(conn, token_signoff) == (sorted([bob.actor, carol.actor, olga.actor]), True)
    async with pool.connection() as conn:
        with pytest.raises(repo.ApprovalRefused, match="four-eyes"):  # the spec approval's authors
            await repo.decide_approval(conn, signoff, bob.actor, approve=True)
    async with pool.connection() as conn:
        with pytest.raises(repo.ApprovalRefused, match="four-eyes"):  # the run's creator
            await repo.decide_approval(conn, signoff, dave.actor, approve=True)
    async with pool.connection() as conn:
        row = await approve(conn, signoff, carol)  # the spec approval's decider is not its author
    assert (row["status"], row["authors"]) == ("approved", sorted([bob.actor, dave.actor, olga.actor]))


async def test_a_signoff_citing_an_approval_that_is_not_approved_is_refused(
    pool: AsyncConnectionPool,
) -> None:
    async with pool.connection() as conn:
        bob, carol, dave = [await person(conn, n) for n in ("bob", "carol", "dave")]
        spec_id = await upload(conn, V1, bob)
        spec_approval = await approved(conn, spec_id, bob, carol)
        run_id = await start(conn, spec_id, dave, approval_id=spec_approval)
        await end(conn, run_id, counters(1))
        signoff = await request_signoff(conn, run_id, 1, dave, V1)
        await repo.revoke_approval(conn, spec_approval, carol.actor, "wrong clear path")
    async with pool.connection() as conn:
        with pytest.raises(repo.ApprovalRefused, match="cites is not approved") as refused:
            await repo.decide_approval(conn, signoff, bob.actor, approve=True)
    assert refused.value.check == "cited"


@pytest.mark.parametrize(
    ("sql", "error", "message"),
    [
        ("DELETE FROM ds.approvals", psycopg.errors.RestrictViolation, "cannot be deleted"),
        ("TRUNCATE ds.approvals CASCADE", psycopg.errors.RaiseException, "approvals is append-only"),
        ("UPDATE ds.approvals SET status = 'revoked', revoked_by = 'x', revoked_at = now(),"
         " revoke_reason = 'x'", psycopg.errors.RestrictViolation, "cannot go from requested to revoked"),
        ("UPDATE ds.approvals SET scopes = '{clear}'", psycopg.errors.RestrictViolation,
         "or change what it covers"),
        ("UPDATE ds.approvals SET requested_by = %(carol)s", psycopg.errors.RestrictViolation,
         "or change what it covers"),
        ("INSERT INTO ds.approvals (approval_id, kind, spec_id, requested_by, status, decided_by, decided_at,"
         " authors, unattributed) SELECT 'A-' || repeat('Z', 26), 'spec', spec_id, requested_by, 'approved',"
         " %(carol)s, now(), '{}', false FROM ds.approvals",
         psycopg.errors.CheckViolation, "must start as an undecided request"),
        ("INSERT INTO ds.approvals (approval_id, kind, spec_id, requested_by, authors)"
         " SELECT 'A-' || repeat('Z', 26), 'spec', 2, requested_by, '{}' FROM ds.approvals",
         psycopg.errors.CheckViolation, "must start as an undecided request"),
        ("INSERT INTO ds.approvals (approval_id, kind, spec_id, requested_by)"
         " SELECT 'A-' || repeat('Z', 26), 'spec', 2, 'local:ann@host' FROM ds.approvals",
         psycopg.errors.CheckViolation, "approvals_requested_by_check"),  # the CLI cannot request
        ("UPDATE ds.approvals SET status = 'approved', decided_by = 'token:T-' || repeat('0', 16),"
         " decided_at = now()", psycopg.errors.CheckViolation, "is not an enabled rule_approver"),
        ("UPDATE ds.approvals SET decision_note = 'a' || chr(27) || 'b'", psycopg.errors.CheckViolation,
         "domain ds.note"),
        ("UPDATE ds.approvals SET decision_note = repeat('x', 1001)", psycopg.errors.CheckViolation,
         "domain ds.note"),
        ("UPDATE ds.approvals SET decision_note = 'x'", None, None),  # open: the trigger lets it be
    ],
)  # fmt: skip
async def test_the_trigger_and_checks_refuse_what_the_lifecycle_does_not_allow(
    pool: AsyncConnectionPool, sql: str, error: type[Exception] | None, message: str | None
) -> None:
    async with pool.connection() as conn:
        bob, carol = await person(conn, "bob"), await person(conn, "carol")
        await upload(conn, V1, bob)
        await upload(conn, V2, bob)
        await request(conn, 1, bob)
    async with pool.connection() as conn:
        if error is None:
            await conn.execute(sql)  # type: ignore[arg-type]
            return
        with pytest.raises(error, match=message):
            await conn.execute(sql, {"carol": carol.actor})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("UPDATE ds.approvals SET status = 'requested', decided_by = NULL, decided_at = NULL",
         "cannot go from approved to requested"),
        ("UPDATE ds.approvals SET decision_note = 'later'", "or change what it covers"),
        ("UPDATE ds.approvals SET authors = '{}'", "or change what it covers"),
        ("UPDATE ds.approvals SET first_by = %(dave)s, first_at = now()", "or change what it covers"),
        ("UPDATE ds.approvals SET status = 'rejected'", "cannot go from approved to rejected"),
    ],
)  # fmt: skip
async def test_a_decided_approval_keeps_its_decision(
    pool: AsyncConnectionPool, sql: str, message: str
) -> None:
    async with pool.connection() as conn:
        bob, carol, dave, erin = [await person(conn, n) for n in ("bob", "carol", "dave", "erin")]
        approval_id = await request(conn, await upload(conn, V1, TESTER.actor), bob)
        await approve(conn, approval_id, carol, erin)
    async with pool.connection() as conn:
        with pytest.raises(psycopg.errors.RestrictViolation, match=message):
            await conn.execute(sql, {"dave": dave.actor})  # type: ignore[arg-type]


async def test_the_first_approver_is_kept(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        bob, carol, dave = [await person(conn, n) for n in ("bob", "carol", "dave")]
        approval_id = await request(conn, await upload(conn, V1, TESTER.actor), bob)
        await repo.decide_approval(conn, approval_id, carol.actor, approve=True, first=True)
        assert await repo.decide_approval(conn, approval_id, dave.actor, approve=True, first=True) is None
    async with pool.connection() as conn:
        with pytest.raises(psycopg.errors.RestrictViolation, match="or change what it covers"):
            await conn.execute("UPDATE ds.approvals SET first_by = %s", (dave.actor,))


async def test_a_version_has_one_open_request_and_one_active_approval(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        bob, carol = await person(conn, "bob"), await person(conn, "carol")
        spec_id = await upload(conn, V1, bob)
        first = await request(conn, spec_id, bob)
        assert await repo.create_approval(conn, "spec", spec_id, carol.actor) is None
        await approve(conn, first, carol)
        second = await repo.create_approval(conn, "spec", spec_id, bob.actor)  # asked again: refused later
        assert second is not None
        with pytest.raises(psycopg.errors.UniqueViolation, match="approvals_active_spec"):
            await approve(conn, second["approval_id"], carol)


async def test_a_run_records_only_a_spec_approval_of_its_own_version(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        bob, carol, dave = [await person(conn, n) for n in ("bob", "carol", "dave")]
        spec_id, other = await upload(conn, V1, bob), await upload(conn, V2, bob)
        spec_approval = await approved(conn, spec_id, bob, carol)
        other_approval = await approved(conn, other, bob, carol)
        run_id = await start(conn, spec_id, dave, approval_id=spec_approval)
        await end(conn, run_id, counters(1))
        signoff = await request_signoff(conn, run_id, 1, dave, V1)
    for approval_id in (signoff, other_approval):
        async with pool.connection() as conn:
            with pytest.raises(
                psycopg.errors.ForeignKeyViolation, match="runs_approval_id_spec_id_approval_kind"
            ):
                await conn.execute("UPDATE ds.runs SET approval_id = %s", (approval_id,))
    async with pool.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation, match="runs_approval_kind_check"):
            await conn.execute("UPDATE ds.runs SET approval_kind = 'signoff'")


async def test_a_stored_version_never_changes(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        bob = await person(conn, "bob")
        spec_id = await upload(conn, V1, bob)
        assert await repo.save_spec(conn, V1, created_by=TESTER.actor) == spec_id  # the no-op upsert passes
    for sql, error in [
        ("UPDATE ds.specs SET body = body || '{\"knownDiffs\": []}'", psycopg.errors.RestrictViolation),
        ("UPDATE ds.specs SET created_by = 'user:U-0000000000000000'", psycopg.errors.RestrictViolation),
        ("DELETE FROM ds.specs", psycopg.errors.RestrictViolation),
        ("TRUNCATE ds.specs CASCADE", psycopg.errors.RaiseException),
    ]:
        async with pool.connection() as conn:
            with pytest.raises(error, match=r"spec 1 cannot be changed or deleted|specs is append-only"):
                await conn.execute(sql)  # type: ignore[arg-type]


async def test_a_first_approver_set_with_the_decision_is_checked_too(pool: AsyncConnectionPool) -> None:
    """A writer setting the first approver and approving in one statement: both must be approvers."""
    async with pool.connection() as conn:
        bob, dave = await person(conn, "bob"), await person(conn, "dave")
        ann = await person(conn, "ann", "spec_author")
        await upload(conn, V1, TESTER.actor)  # an unattributed lineage: two approvers
        approval_id = await request(conn, await upload(conn, V2, bob), bob)
    async with pool.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation, match="is not an enabled rule_approver"):
            await conn.execute(
                """UPDATE ds.approvals SET status = 'approved', first_by = %s, first_at = now(),
                   decided_by = %s, decided_at = now() WHERE approval_id = %s""",
                (ann.actor, dave.actor, approval_id),
            )
