"""What refuses a pack: every census basis and refusal, the sign-off's
format and freshness, the adjudication from 50 per-key results, raw key-shaped values in the spec, the
sign-off or an audit row (and reasons refused when written), read with the spec's own key check and
never in DeltaSleuth's own shapes, a broken audit trail (also behind a replaced ``ds.audit_text``), the
size limit, a table-scan run from before 009, a purge (a download too), the modes, and a sign-off revoked
or a purge run while the pack is built."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml

from deltasleuth import approvals
from deltasleuth.config import AuthSettings
from deltasleuth.context import Context
from deltasleuth.core.spec import compile_spec, load_spec_file, validate_spec
from deltasleuth.reports import pack
from deltasleuth.reports.adjudicate import AdjudicationConflict, store_draw
from deltasleuth.runner import census, purge
from deltasleuth.runner.keys import normalise_keys
from deltasleuth.runner.purge import CyclePurged
from deltasleuth.security import rawvalues
from deltasleuth.store import repo
from tests.conftest import TESTER

from .conftest import GRAPH_SPEC, SPEC_YAML, TARGETS
from .packs import LISTING, RAW_KEY, files_of, listed_spec, samples, signed_cycle

pytestmark = pytest.mark.db
AGAIN = "revoke it and request the sign-off again"
AT = "2026-10-01T08:00:00.000000Z"
TABLE = {"schema": "legacy", "name": "accounts", "keys": ["id"], "columns": {"email": "email"}}
LISTED = {"basis": "listing", "target": "svc-a", "items": 6, "first_at": AT, "last_at": AT}
COUNTED = {"basis": "table", "target": "db-a", "relation": "legacy.accounts", "where": None, "rows": 9,
           "skipped": 0, "missing": 0, "extra": 0, "at": AT}  # fmt: skip


async def _problems(ctx: Context, run_id: str, cycle: int = 1) -> list[str]:
    async with ctx.pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
        assert run is not None
        return await pack.problems(ctx, conn, run, cycle)


async def _refused(ctx: Context, run_id: str, who: Any) -> str:
    with pytest.raises(pack.PackRefused) as caught:
        await pack.issue(ctx, run_id, 1, who=who)
    return str(caught.value)


def _run(source: str | None, tables: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"run_id": "R-1", "params": {"keys_source": source, "tables": tables}}


def _census(a: Any = None, b: Any = None, cycle: int = 1, at: str | None = AT) -> dict[str, Any]:
    return {"cycle": cycle, "at": at, "a": a, "b": b}


def test_each_census_basis_and_why_a_cycle_is_not_one() -> None:
    listing, graph = listed_spec(), compile_spec(yaml.safe_load(GRAPH_SPEC), targets=TARGETS)
    of_b = listed_spec(key=yaml.safe_load(yaml.safe_dump(listing.raw["key"])) | {
        "source": yaml.safe_load(LISTING.replace("target: svc-a", "target: svc-b"))["source"]
    })  # fmt: skip
    done, cut = {"done": True}, {"keys_cut": {"taken": 10}}
    not_a = "is not a census: "
    scanned, table_a = _run("table_scan", {"a": TABLE, "b": TABLE}), _run("explicit", {"a": TABLE})
    lacks = "its key set lacks 12 key(s) of side a's table legacy.accounts (counted 2026-10-01 08:00 UTC)"
    cases = [  # run, spec, census, scan, ended, last: the refusal, if any
        (_run("paginated_endpoint"), listing, _census(LISTED), None, None, None, None),
        (scanned, graph, _census({"basis": "scan"}, {"basis": "scan"}), done, {}, None, None),
        (table_a, graph, _census(COUNTED), None, None, None, None),
        (table_a, graph, _census(COUNTED | {"missing": 12}), None, None, None, f"{not_a}{lacks}"),
        (table_a, graph, _census(), None, None, None, f"{not_a}side a's key census is pending"),
        (_run("list", {"a": TABLE}), graph, _census({"failed": "QueryCanceled"}), None, None, None,
         f"{not_a}side a's key census failed (QueryCanceled)"),
        (_run("explicit", {"b": TABLE}), graph, _census(None, COUNTED), None, None, None,
         f"{not_a}side a (the baseline) was not counted; a count of side b cannot show keys side b lost"),
        (_run("explicit"), listing, _census(), None, None, None,
         f"{not_a}its keys were given with the run and no side is a table to check them against"),
        (_run("list"), listing, _census(), None, None, None,
         f"{not_a}its keys are the spec's inline list and no side is a table to check them against"),
        (_run("paginated_endpoint"), of_b, _census(None, LISTED | {"target": "svc-b"}), None, None, None,
         f"{not_a}its key listing reads target svc-b, not side a's"),
        (_run("table_scan", {"a": TABLE, "b": TABLE}), graph, _census(), done, {}, None,
         f"{not_a}graph parity (L2) did not run"),
        # A scan stopped early or cut by maxKeys counts no side: the sign-off's own problems say why.
        (_run("table_scan", {"a": TABLE, "b": TABLE}), graph, _census(), {"done": False}, {}, None, None),
        (_run("table_scan", {"a": TABLE, "b": TABLE}), graph, _census(), done, cut, None, None),
    ]  # fmt: skip
    for number, (run, spec, counted, scan, ended, last, why) in enumerate(cases):
        found = pack.census_problem(run, 1, spec, counted, scan, ended, last)
        assert found == (why and f"cycle 1 of run R-1 {why}"), number
    own = "has no census of its own (last counted 2026-09-01, at cycle 1)"
    another = _census(LISTED, cycle=1, at="2026-09-01T10:00:00.000000Z")
    assert pack.census_problem(_run("paginated_endpoint"), 5, listing, another, None, None, None) == (
        f"cycle 5 of run R-1 {own}"
    )
    walked_once = {"cycle": 1, "census": another}  # a non-rolling soak's listing is walked once
    assert pack.census_problem(_run("paginated_endpoint"), 5, listing, _census(cycle=5, at=None), None, None,
                               walked_once) == f"cycle 5 of run R-1 {own}"  # fmt: skip


def test_key_table_is_no_census() -> None:
    """A key table is a key list, not a census of a side: its read's record counts no side, whichever target
    it reads, so between two services no pack follows from its run; beside a table side that side's key
    census checks the list, as it checks any other."""
    spec = listed_spec()
    read = {"basis": "key_table", "target": "svc-a", "relation": "ref.keys", "rows": 6, "keys": 6, "at": AT}
    assert census.listed(spec, read, 1) is None and census.listed(spec, LISTED, 1) is not None
    assert pack.census_problem(_run("table"), 1, spec, _census(), None, None, None) == (
        "cycle 1 of run R-1 is not a census: its keys are the spec's key table's and no side is a table to"
        " check them against"
    )
    beside = _run("table", {"a": TABLE})
    assert pack.census_problem(beside, 1, spec, _census(), None, None, None) == (
        "cycle 1 of run R-1 is not a census: side a's key census is pending"
    )
    assert pack.census_problem(beside, 1, spec, _census(COUNTED), None, None, None) is None


async def test_a_signed_census_cycle_has_no_problem_until_its_sign_off_is_revoked(pack_ctx: Context) -> None:
    signed = await signed_cycle(pack_ctx)
    assert await _problems(pack_ctx, signed.run_id) == []
    async with pack_ctx.pool.connection() as conn:
        await approvals.revoke(conn, partial(repo.insert_audit, conn), signed.signoff_id, "redo", who=TESTER)
    unsigned = f"cycle 1 of run {signed.run_id} has no approved sign-off"
    assert await _problems(pack_ctx, signed.run_id) == [unsigned]
    async with pack_ctx.pool.connection() as conn:  # as if the run had been created before 009
        await conn.execute(
            "UPDATE ds.schema_version SET applied_at = now() + interval '1 day' WHERE version = 9"
        )
    before = f"run {signed.run_id} started before evidence packs (009): only later runs get a pack"
    assert await _refused(pack_ctx, signed.run_id, signed.people["olga"]) == f"{before}; {unsigned}"


async def test_a_table_scan_run_started_before_009_is_refused_not_built(pack_ctx: Context) -> None:
    # 008's scan state, read to its end: no snapshots_a, snapshots_b, first_at or last_at
    state = {"done": True, "after": None, "rows_a": 6, "rows_b": 6, "skipped_a": 0, "skipped_b": 0,
             "matched": 0, "candidates": 6, "checkpoint": 1}  # fmt: skip
    signed = await signed_cycle(pack_ctx, scan=state)
    async with pack_ctx.pool.connection() as conn:  # created before 009, signed off after it
        await conn.execute(
            "UPDATE ds.schema_version SET applied_at = now() + interval '1 day' WHERE version = 9"
        )
        signoff = await repo.get_approval(conn, signed.signoff_id)
        run = await repo.get_run(conn, signed.run_id)
        assert run is not None
        judged = await pack._judged(pack_ctx, conn, run, 1, None)
    assert signoff is not None and signoff["attests"]["format"] == 3
    before = f"run {signed.run_id} started before evidence packs (009): only later runs get a pack"
    assert judged == ([before], None)  # the pack is not built
    assert await _problems(pack_ctx, signed.run_id) == [before]
    assert await _refused(pack_ctx, signed.run_id, signed.people["olga"]) == before
    counted = signoff["attests"]["census"]["a"]
    assert (counted["basis"], counted["snapshots"], counted["first_at"]) == ("scan", None, None)
    assert census.described(counted) == "read to its end; snapshots not recorded (before 009)"
    assert census.warning("a", counted) is None
    assert census.warning("a", counted | {"snapshots": 3}) == (
        "SIDE A READ IN 3 SNAPSHOTS between - and -: rows added behind a resume point meanwhile were not read"
    )


async def test_a_format_0_sign_off_and_a_stale_one_are_refused(pack_ctx: Context) -> None:
    signed = await signed_cycle(pack_ctx)
    run_id, signoff = signed.run_id, signed.signoff_id
    async with pack_ctx.pool.connection() as conn:  # a fact the sign-off attests changed since
        await conn.execute(
            "UPDATE ds.cycles SET census = jsonb_set(census, '{a,items}', '7') WHERE run_id = %s", (run_id,)
        )
    stale = (
        f"the sign-off {signoff} of run {run_id} cycle 1 is stale (rules changed since it was signed under"
    )
    assert await _problems(pack_ctx, run_id) == [f"{stale} attests format 3): {AGAIN}"]
    async with pack_ctx.pool.connection() as conn:
        await approvals.revoke(conn, partial(repo.insert_audit, conn), signoff, "stale", who=TESTER)
        run = await repo.get_run(conn, run_id)
        assert run is not None
        facts = await approvals._format_0(conn, run, 1, signed.spec)  # as 008 requested it
        request = await repo.create_approval(
            conn, "signoff", signed.spec_id, signed.people["olga"].actor, run_id=run_id, cycle=1,
            attests=facts, attests_sha256=approvals.attests_sha256(facts),
        )  # fmt: skip
        assert request is not None
        await repo.decide_approval(conn, request["approval_id"], signed.people["erin"].actor, approve=True)
    before = f"the sign-off {request['approval_id']} of run {run_id} cycle 1 was signed before evidence packs"
    assert await _problems(pack_ctx, run_id) == [f"{before} (attests format 0): {AGAIN}"]


async def test_from_50_results_a_passed_adjudication_is_required(pack_ctx: Context) -> None:
    unadjudicated = await signed_cycle(pack_ctx, pairs=samples(52))
    assert await _problems(pack_ctx, unadjudicated.run_id) == [
        f"cycle 1 of run {unadjudicated.run_id} has no scored adjudication in its sign-off"
        f" {unadjudicated.signoff_id}: revoke it, draw and score a reviewer sheet, then request the sign-off"
        " again"
    ]
    version = listed_spec(metadata={"name": "unit-reports", "version": "1.0.1", "owner": "tests"})
    failed = await signed_cycle(pack_ctx, spec=version, pairs=samples(52), adjudicate=True, disagree=True)
    assert await _problems(pack_ctx, failed.run_id) == [
        f"the attested adjudication {failed.adjudication_id} did not pass (1 disagreement(s) between"
        " reviewers and the engine): no pack"
    ]
    # The sign-off froze the draws, and the scorer is kept from approving it.
    frozen = f"cycle 1 of run {failed.run_id} has a sign-off ({failed.signoff_id}): revoke it before a new"
    with pytest.raises(AdjudicationConflict, match=re.escape(frozen)):
        await store_draw(pack_ctx, failed.run_id, 1, who=failed.people["olga"])
    async with pack_ctx.pool.connection() as conn:
        authors = await repo.approval_authors(conn, failed.signoff_id)
    assert authors is not None and failed.people["frank"].actor in authors["persons"]


@pytest.mark.parametrize("pattern", ["^[0-9]{10}$", "^[0-9]{4,12}$"])
async def test_raw_key_shaped_values_refuse_a_pack(pack_ctx: Context, pattern: str) -> None:
    rule = {"id": "KD-1", "when": f"path == '$.status' && a == '{RAW_KEY}'", "classification": "accepted",
            "reason": "status vocabulary renamed", "ticket": "UNIT-2"}  # fmt: skip
    key = listed_spec(pii=True).raw["key"] | {"pattern": pattern}
    spec = listed_spec(pii=True, key=key, knownDiffs=[rule])
    signed = await signed_cycle(pack_ctx, spec=spec, pairs=samples(pii=True))
    assert await _problems(pack_ctx, signed.run_id) == [  # in the spec and in the sign-off's rules
        "the pack's texts hold 2 raw key-shaped value(s) (known_diffs[0].when): no pack carries them"
    ]


@pytest.mark.parametrize("pattern", ["^[0-9]{4,12}$", "^[0-9]{1,10}$"])
async def test_a_numeric_key_pattern_gets_a_pack_unless_a_text_holds_such_a_number(
    pack_ctx: Context, pattern: str
) -> None:
    key = listed_spec(pii=True).raw["key"] | {"pattern": pattern}
    spec = listed_spec(pii=True, key=key, knownDiffs=[])  # a rule's ticket (UNIT-1) holds a number
    signed = await signed_cycle(pack_ctx, spec=spec, pairs=samples(50, pii=True), adjudicate=True)
    # Its times, dates, versions, ids, digests, salt, counts, addresses and fixed texts are no keys.
    assert await _problems(pack_ctx, signed.run_id) == []
    issued = await pack.issue(pack_ctx, signed.run_id, 1, who=signed.people["olga"])
    assert issued["pack_id"].startswith("P-")
    await purge.hold(pack_ctx, TESTER, signed.run_id, None)  # the sign-off's hold released
    with pytest.raises(ValueError, match=re.escape(rawvalues.KEY_SHAPED)):
        await purge.hold(pack_ctx, TESTER, signed.run_id, "customer 4711 asked")
    async with pack_ctx.pool.connection() as conn:  # a reason written past the check, as psql could
        row = await repo.insert_audit(
            conn, TESTER, "retention.hold", signed.run_id, {"reason": "customer 4711 asked"}
        )
    assert await _problems(pack_ctx, signed.run_id) == [
        f"the pack's texts hold 1 raw key-shaped value(s) (audit row {row['chain']['seq']}): no pack carries"
        " them"
    ]


@pytest.mark.parametrize("pattern", ["^[0-9]{4,30}$", "^[0-9]{1,10}$"])
async def test_scheduled_run_pack_passes_gate(pack_ctx: Context, pattern: str) -> None:
    """A schedule's run: its plan and its ``create_run`` row name the schedule,
    whose id is one DeltaSleuth writes, the due time and the zone; no row of the schedule itself is in its
    pack's segment."""
    schedule_id = "SC-" + "0123456789" * 2 + "012345"  # all digits: its parts would match either pattern
    fired = {"schedule_id": schedule_id, "due": "2026-10-07T01:00Z", "revision": 4}
    key = listed_spec(pii=True).raw["key"] | {"pattern": pattern}
    spec = listed_spec(pii=True, key=key, knownDiffs=[])
    signed = await signed_cycle(
        pack_ctx, spec=spec, pairs=samples(pii=True), schedule=fired | {"time_zone": "Europe/London"}
    )
    assert await _problems(pack_ctx, signed.run_id) == []
    issued = await pack.issue(pack_ctx, signed.run_id, 1, who=signed.people["olga"])
    async with pack_ctx.pool.connection() as conn:
        files = files_of(await repo.pack_zip(conn, issued["pack_id"]))  # type: ignore[arg-type]
        run = await repo.get_run(conn, signed.run_id)
    plan = json.loads(files["facts/plan.json"])
    assert plan["schedule"] == fired | {"time_zone": "Europe/London"} == run["params"]["schedule"]  # type: ignore[index]
    rows = [json.loads(line.split("\t", 5)[5]) for line in files["audit/chain.tsv"].decode().split("\n")[:-1]]
    (created,) = [row for row in rows if row["action"] == "create_run"]
    assert (created["actor"], created["detail"]["schedule"]) == ("system:worker", plan["schedule"])
    assert created["detail"]["plan_sha256"] == hashlib.sha256(files["facts/plan.json"]).hexdigest()
    # No row of the schedule: a pack's audit segment is unchanged, and its rows stay in the installation's
    # chain.
    assert schedule_id not in {row["object"] for row in rows} and "schedule.fire" not in {
        row["action"] for row in rows
    }
    # A zone named by its offset holds a number such a key pattern may match: its runs get no pack.
    other = listed_spec(
        pii=True, key=key, knownDiffs=[], metadata={"name": "unit-offset", "version": "1.0.0", "owner": "t"}
    )
    offset = fired | {"schedule_id": "SC-" + "7" * 26, "time_zone": "Etc/GMT+5"}
    refused = await signed_cycle(pack_ctx, spec=other, pairs=samples(pii=True), schedule=offset)
    said = await _problems(pack_ctx, refused.run_id)
    if pattern == "^[0-9]{1,10}$":
        assert len(said) == 1 and said[0].startswith("the pack's texts hold 2 raw key-shaped value(s) (")
    else:
        assert said == []


async def test_a_key_shaped_audit_row_and_an_empty_pattern_refuse_a_pack(pack_ctx: Context) -> None:
    signed = await signed_cycle(pack_ctx, spec=listed_spec(pii=True), pairs=samples(pii=True))
    assert await _problems(pack_ctx, signed.run_id) == []  # tokens, hashes, ids and counts are not keys
    async with pack_ctx.pool.connection() as conn:  # a reason written past the check, as psql could
        row = await repo.insert_audit(
            conn, TESTER, "retention.hold", signed.run_id, {"reason": f"for {RAW_KEY}"}
        )
    assert await _problems(pack_ctx, signed.run_id) == [
        f"the pack's texts hold 1 raw key-shaped value(s) (audit row {row['chain']['seq']}): no pack carries"
        " them"
    ]
    key = listed_spec(pii=True).raw["key"] | {"pattern": "^[0-9]*$"}
    unbounded = listed_spec(
        pii=True, key=key, metadata={"name": "unit-reports", "version": "2.0.0", "owner": "t"}
    )
    other = await signed_cycle(pack_ctx, spec=unbounded, pairs=samples(pii=True))
    assert await _problems(pack_ctx, other.run_id) == [
        f"spec {other.spec_id}'s key.pattern matches the empty string: a raw key cannot be told from other"
        " text, so no pack"
    ]


def test_no_bundled_spec_is_one_no_pack_is_issued_for() -> None:
    """The tables example shipped identifier keys without a key.pattern: a copy of it, approved, run and
    signed off, was refused its pack at the last step. Every bundled spec's keys are business codes or told
    by its pattern, and none is warned of."""
    root = Path(__file__).parents[3]
    bundled = [*root.glob("examples/**/spec.yaml"), *root.glob("mocks/*/spec.yaml")]
    assert root / "examples" / "tables" / "spec.yaml" in bundled and len(bundled) >= 9
    for path in bundled:
        doc = load_spec_file(path)
        assert rawvalues.no_pack(0, compile_spec(doc)) is None, path
        assert "key_pattern_empty" not in {issue.code for issue in validate_spec(doc)}, path


def test_the_gate_names_where_a_raw_key_is_and_skips_tokens_hashes_ids_and_numbers() -> None:
    spec = listed_spec(pii=True)
    body = {
        "sides": {"a": {"table": {"where": f"id <> '{RAW_KEY}'"}}},
        "compare": {"valueFilter": f"x != '{RAW_KEY}'"},
        "knownDiffs": [{"when": "a == 'x'"}],
    }
    row = json.dumps({"id": 7, "x": RAW_KEY})
    clean = {
        "token": f"tok:identifier:v1:{'A' * 26}",
        "hash": "0712345678" + "a" * 54,
        "ids": ["R-0712345678ABCDEFGHJKMNPQRS", "O-0712345678abcdef"],
        "epoch": 1790000000,
        "at": "2026-09-28T10:00:00.071234Z",
    }
    files = {
        "facts/spec.json": json.dumps(body).encode(),
        "manifest.json": json.dumps(clean).encode(),
        "csv/graph.csv": f"seq,label\r\n1,x {RAW_KEY} y\r\n2,{RAW_KEY}\r\n".encode(),  # a lone number: counts
        "audit/chain.tsv": "\t".join(["7", "0" * 64, "0" * 64, "1" * 64, "R-1", row]).encode() + b"\n",
        "VERIFY.txt": f"HEAD={RAW_KEY}\n".encode(),  # fixed text, ids and row numbers: not searched
    }  # fmt: skip
    assert pack.raw_values(spec, files) == [
        "sides.a.table.where", "compare.value_filter", "audit row 7", "csv/graph.csv line 2",
    ]  # fmt: skip
    assert pack.raw_values(listed_spec(), files) == []  # business_code keys are stored in clear by design


@pytest.mark.parametrize(
    ("pattern", "normalize", "raw"),
    [
        (r"\A[0-9]{10}\Z", (), RAW_KEY),  # Python's anchors
        (r"(?i)^[a-z]{2}[0-9]{8}$", (), "gb12345678"),  # a leading inline flag
        (r"(?x) ^ [0-9]{10} $", (), RAW_KEY),
        (r"[0-9]{10}", (), "071234567801"),  # unanchored: a key of 12 digits matches by search
        (r"^[0-9]{10}$", ("digits_only",), "071-234-5601"),  # the raw form of 0712345601
        (r"^[A-Z]{2}[0-9]{8}$", ("trim", "upper"), "gb12345678"),  # of GB12345678
        (r"^[0-9]{16}$", (), "4111111111111111"),  # sixteen digits: a card number, not a hash
    ],
)
def test_the_gate_reads_key_pattern_as_keys_are_read(
    pattern: str, normalize: tuple[str, ...], raw: str
) -> None:
    key = (
        listed_spec(pii=True).raw["key"]
        | {"pattern": pattern}
        | ({"normalize": list(normalize)} if normalize else {})
    )
    spec = listed_spec(pii=True, key=key)
    assert normalise_keys([raw], spec)  # a key of the spec, as written
    body = {"knownDiffs": [{"when": f"path == '$.id' && a == '{raw}'"}]}
    assert pack.raw_values(spec, {"facts/spec.json": json.dumps(body).encode()}) == ["known_diffs[0].when"]
    assert rawvalues.key_shaped(spec, f"customer {raw}, asked on 2026-09-28")
    assert not rawvalues.key_shaped(spec, "legal hold LH-7 since 2026-09-28T10:00:00.071234Z")
    with pytest.raises(rawvalues.EmptyPattern):  # a table-only spec may have none
        rawvalues.searcher(replace(spec, key=replace(spec.key, pattern=None)))


def test_gate_finds_a_composite_part() -> None:
    """A multi-part key's raw part is found, alone or in the joined key: the gate's words end at ``|``, so
    each part is a word, judged by the pattern every part fits. A reason is read the same way."""
    joined = f"GB07|{RAW_KEY}"
    key = listed_spec(pii=True).raw["key"] | {
        "type": "composite", "parts": ["branch", "account"], "pattern": "^([A-Z]{2}[0-9]{2}|[0-9]{10})$",
        "source": {"kind": "list", "keys": [joined]},
    }  # fmt: skip
    spec = listed_spec(pii=True, key=key)
    assert spec.key_parts == ("branch", "account") and normalise_keys([joined], spec) == [joined]
    for raw, words in ((joined, 2), (RAW_KEY, 1), ("GB07", 1)):  # the joined key: a word a part
        body = {"knownDiffs": [{"when": f"path == '$.id' && a == '{raw}'"}]}
        found = pack.raw_values(spec, {"facts/spec.json": json.dumps(body).encode()})
        assert found == ["known_diffs[0].when"] * words, raw
        assert rawvalues.key_shaped(spec, f"customer {raw}, asked on 2026-09-28"), raw
    assert not rawvalues.key_shaped(spec, "legal hold LH-7 since 2026-09-28T10:00:00.071234Z")


@pytest.mark.parametrize(
    "text",
    [
        f"customer \u201c{RAW_KEY}\u201d asked", f"\u00ab{RAW_KEY}\u00bb", f"{RAW_KEY}%", f"id${RAW_KEY}",
        f"x&{RAW_KEY}", f"path\\{RAW_KEY}", f"ref\u2013{RAW_KEY}", f"a\u200b{RAW_KEY}", f"{RAW_KEY}\u2019s",
        f"{RAW_KEY}\u3002", f"{RAW_KEY}\u2026",
    ],
)  # fmt: skip
def test_a_key_next_to_any_punctuation_or_symbol_is_found(text: str) -> None:
    spec = listed_spec(pii=True)
    assert rawvalues.key_shaped(spec, text)
    assert pack.raw_values(spec, {"x.json": json.dumps({"reason": text}).encode()}) == ["x.json reason"]


def test_digit_groups_a_space_splits_are_one_key_under_digits_only() -> None:
    key = listed_spec(pii=True).raw["key"] | {"normalize": ["digits_only"]}
    spec = listed_spec(pii=True, key=key)
    a, b, c = RAW_KEY[:3], RAW_KEY[3:6], RAW_KEY[6:]
    for text in (f"customer {a} {b} {c}", f"({a}) {b}-{c}"):
        assert rawvalues.key_shaped(spec, text), text
    for fine in ("hold until 2026-10-01 10 am", "step 1 of 2, LH-7", f"+44 {RAW_KEY}9"):
        assert not rawvalues.key_shaped(spec, fine), fine
    assert not rawvalues.key_shaped(listed_spec(pii=True), f"customer {a} {b} {c}")  # no digits_only


def test_what_deltasleuth_writes_itself_is_never_a_key() -> None:
    key = listed_spec(pii=True).raw["key"] | {"pattern": "^[0-9]{1,10}$"}
    spec = listed_spec(pii=True, key=key, knownDiffs=[])  # its versions, apiVersion and key.pattern
    at, fixed = "2026-09-28T10:00:00.071234Z", "ECDSA P-256, SHA-256 (RFC 6979)"
    manifest = {
        "format": pack.FORMAT, "procedure": pack.PROCEDURE_NAME, "installation": "prod-eu-1",
        "claim_text": pack.PROVES.format(name="prod-eu-1", fp="0" * 64), "signing_key": {"algorithm": fixed},
        "issued_at": at, "auth_time": "2026-09-28T10:00:00.071234+02:00", "until": "2027-10-02",
        "counters": {"coverage": "0.9985", "2026": "1"}, "audit": {"objects": ["1234", "R-" + "0" * 26]},
        "ids": ["user:U-0123456789012345", "W-0123456789012345", "RC-012345678901", "J-" + "7" * 26,
                "D-" + "7" * 26],
        "digests": ["0712345678" + "a" * 54, "sha256:" + "1" * 63 + "f", "2" * 31 + "c", "3685c5094a6b6347"],
        "version": "10.2.3",
        # A sampled pack's: the nonce, its sampled claim, u_max, bounds, labels.
        "sample": {"nonce": "6d23080bff0fd27a555cfdbc3cbc0a48", "draw_id": "D-" + "8" * 26,
                   "strata": [{"label": "kind=ORG|flagged=Y", "u_max": "18446744073709551615",
                               "target": "0.0001", "ub95_one_sided": "0.00009999"}]},
    }  # fmt: skip
    manifest["claim_text"] = pack.PROVES_SAMPLED.format(name="prod-eu-1", fp="0" * 64, absent="12")
    detail = {"reason": f"signed off: A-{'0' * 26}", "via": "2001:db8::1", "to": "::1"}
    row = json.dumps({"at": at, "object": "1234", "client_addr": "10.1.2.3", "detail": detail})
    files = {
        "facts/spec.json": json.dumps(spec.raw).encode(),
        "manifest.json": json.dumps(manifest).encode(),
        "audit/chain.tsv": "\t".join(["7", "0" * 64, "0" * 64, "1" * 64, "1234", row]).encode() + b"\n",
    }
    assert pack.raw_values(spec, files) == []
    written = spec.raw | {"knownDiffs": [{"when": "a == '4711'"}], "compare": {"columns": {"4711": "x"}}}
    files["facts/spec.json"] = json.dumps(written).encode()
    assert pack.raw_values(spec, files) == ["compare.columns.4711", "known_diffs[0].when"]
    files["csv/coverage.csv"] = (
        b"stratum,tier\r\n*,\r\nsegment=4711|flagged=Y,standard\r\n"  # split at = and |
    )
    assert pack.raw_values(spec, files)[2:] == ["csv/coverage.csv line 3"]
    assert rawvalues.key_shaped(spec, "legal hold LH-2026") and not rawvalues.key_shaped(spec, at)


def test_inline_integer_keys_are_searched_as_their_decimal_text() -> None:
    raw = yaml.safe_load(SPEC_YAML)
    raw["key"] = {"name": "account_no", "type": "integer", "pattern": "^[0-9]{9}$", "class": "identifier",
                  "source": {"kind": "list", "keys": [712345601, 712345602]}}  # fmt: skip
    raw["security"]["clear"] = ["$.status", "$.tags[*]"]
    spec = compile_spec(raw, targets=TARGETS)
    stored = {"facts/spec.json": json.dumps(spec.raw).encode()}
    assert pack.raw_values(spec, stored) == ["key.source.keys[0]", "key.source.keys[1]"]
    union = {"key": {"source": {"kind": "union", "sources": [{"kind": "list", "keys": [True, 712345601]}]}}}
    assert pack.raw_values(spec, {"facts/spec.json": json.dumps(union).encode()}) == [
        "key.source.sources[0].keys[1]"
    ]  # not a boolean


async def test_hold_erasure_and_revoke_reasons_holding_a_key_are_refused_when_written(
    pack_ctx: Context,
) -> None:
    signed = await signed_cycle(pack_ctx, spec=listed_spec(pii=True), pairs=samples(pii=True))
    run_id = signed.run_id
    await purge.hold(pack_ctx, TESTER, run_id, None)  # the sign-off's hold released
    with pytest.raises(ValueError, match=re.escape(rawvalues.KEY_SHAPED)):
        await purge.hold(pack_ctx, TESTER, run_id, f"customer {RAW_KEY} asked")
    with pytest.raises(ValueError, match=re.escape(rawvalues.KEY_SHAPED)):
        await purge.erase(pack_ctx, TESTER, run_id, f"erase {RAW_KEY}", cycle=1)
    assert rawvalues.key_shaped(signed.spec, "ask 1234567890")
    for fine in (f"tok:identifier:v1:{'A' * 26}", "12345678901", "123456789", "a1234567890"):
        assert not rawvalues.key_shaped(signed.spec, fine), fine
    await purge.hold(pack_ctx, TESTER, run_id, "legal hold LH-7")  # no key: held
    async with pack_ctx.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM ds.audit_log WHERE action IN ('retention.hold', 'retention.expire')"
            " AND detail->>'reason' LIKE %s",
            (f"%{RAW_KEY}%",),
        )
        assert (await cur.fetchone())[0] == 0  # type: ignore[index]


async def test_a_broken_audit_trail_and_a_pack_over_the_limit_are_refused(
    pack_ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    signed = await signed_cycle(pack_ctx)
    monkeypatch.setattr(pack, "MIB", 100)  # 100 MiB of 100 bytes: 10,000 bytes
    monkeypatch.setattr(pack, "MAX_BYTES", 100 * 100)
    [over] = await _problems(pack_ctx, signed.run_id)
    assert re.fullmatch(r"the pack would be [0-9]+ MiB, over the 100 MiB limit", over), over
    monkeypatch.undo()
    async with pack_ctx.pool.connection() as conn:  # the database owner edits the run's creation row
        cur = await conn.execute(
            """SELECT c.seq FROM ds.audit_chain c JOIN ds.audit_log l USING (id)
               WHERE l.action = 'create_run' AND l.object = %s""",
            (signed.run_id,),
        )
        (seq,) = await cur.fetchone()  # type: ignore[misc]
        await conn.execute("ALTER TABLE ds.audit_log DISABLE TRIGGER audit_log_append_only")
        await conn.execute(
            """UPDATE ds.audit_log SET detail = detail || '{"keys": 5}'
               WHERE id = (SELECT id FROM ds.audit_chain WHERE seq = %s)""",
            (seq,),
        )
        await conn.execute("ALTER TABLE ds.audit_log ENABLE TRIGGER audit_log_append_only")
    assert await _problems(pack_ctx, signed.run_id) == [
        f"the audit trail of run {signed.run_id} does not verify at row {seq}: run ds audit verify"
    ]


async def test_an_edited_grant_covered_by_a_replaced_audit_text_is_refused(pack_ctx: Context) -> None:
    signed = await signed_cycle(pack_ctx)
    async with pack_ctx.pool.connection() as conn:  # the database owner edits the sign-off's grant
        cur = await conn.execute(
            """SELECT c.seq FROM ds.audit_chain c JOIN ds.audit_log l USING (id)
               WHERE l.action = 'approval.grant' AND l.object = %s""",
            (signed.signoff_id,),
        )
        (seq,) = await cur.fetchone()  # type: ignore[misc]
        await conn.execute("SET LOCAL session_replication_role = replica")
        await conn.execute(
            """UPDATE ds.audit_log SET actor = 'user:U-ffffffffffffffff', actor_name = 'mallory'
               WHERE id = (SELECT id FROM ds.audit_chain WHERE seq = %s)""",
            (seq,),
        )
        await conn.execute(  # and has ds.audit_text return the stored text
            """CREATE OR REPLACE FUNCTION ds.audit_text(a ds.audit_log) RETURNS text LANGUAGE sql STABLE AS $$
                   SELECT c.text FROM ds.audit_chain c WHERE c.id = a.id $$"""
        )
    assert await _refused(pack_ctx, signed.run_id, signed.people["olga"]) == (
        f"the audit trail of run {signed.run_id} does not verify at row {seq}: run ds audit verify"
    )


async def test_a_purge_the_modes_and_the_issuer_come_first(pack_ctx: Context) -> None:
    signed = await signed_cycle(pack_ctx)
    olga = signed.people["olga"]
    with pytest.raises(pack.PackUnavailable, match=re.escape(pack.NOT_CONFIGURED)):
        await pack.issue(replace(pack_ctx, signer=None), signed.run_id, 1, who=olga)
    for mode, auth in (("tokens mode", AuthSettings("https://ds.test")), ("developer mode", None)):
        other = replace(pack_ctx, settings=replace(pack_ctx.settings, auth=auth), signer=None)
        said = f"evidence packs need SSO (auth.oidc); this deployment runs in {mode}"
        assert await _refused(other, signed.run_id, olga) == said  # before the missing key
    with pytest.raises(pack.PackForbidden, match="issuing a pack needs a signed-in person"):
        await pack.issue(pack_ctx, signed.run_id, 1, who=TESTER)
    issued = await pack.issue(pack_ctx, signed.run_id, 1, who=olga)
    async with pack_ctx.pool.connection() as conn:
        await repo.set_hold(conn, signed.run_id, None, None)
        started = await repo.start_purge(conn, signed.run_id, 1, "erasure: test")
    assert started is not None
    with pytest.raises(CyclePurged):
        await _problems(pack_ctx, signed.run_id)
    # A download too, once the purge started: its zip is still stored until the purge ends.
    with pytest.raises(CyclePurged) as gone:
        await pack.stored(pack_ctx, issued["pack_id"])
    assert str(gone.value) == (
        f"pack {issued['pack_id']} was purged with cycle 1 on {started['purging_at']:%Y-%m-%d}: its stored"
        " copy is deleted; copies downloaded before still verify"
    )
    async with pack_ctx.pool.connection() as conn:
        assert await repo.pack_zip(conn, issued["pack_id"]) is not None


async def test_a_sign_off_revoked_while_the_pack_is_built_refuses_it(
    pack_ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    signed = await signed_cycle(pack_ctx)
    judged = pack._judged

    async def revoked_meanwhile(*args: Any, **kwargs: Any) -> Any:
        found = await judged(*args, **kwargs)
        async with pack_ctx.pool.connection() as conn:
            await approvals.revoke(
                conn, partial(repo.insert_audit, conn), signed.signoff_id, "late", who=TESTER
            )
        return found

    monkeypatch.setattr(pack, "_judged", revoked_meanwhile)
    assert await _refused(pack_ctx, signed.run_id, signed.people["olga"]) == (
        f"sign-off {signed.signoff_id} was revoked meanwhile"
    )
    async with pack_ctx.pool.connection() as conn:
        assert await repo.cycle_packs(conn, signed.run_id, 1) == []
        with pytest.raises(psycopg.errors.CheckViolation, match="is not approved"):  # any writer
            row = {"pack_id": repo.new_pack_id(), "run_id": signed.run_id, "cycle": 1,
                   "signoff_id": signed.signoff_id, "adjudication_id": None, "manifest": {}, "audit_seq": 1,
                   "issued_at": datetime.now(UTC), "issued_by": signed.people["olga"].actor}  # fmt: skip
            hashes = dict.fromkeys(("sums_sha256", "zip_sha256", "key_fingerprint"), "0" * 64)
            await repo.insert_pack(conn, row | hashes, b"")


async def test_a_purge_while_the_pack_is_built_refuses_it_and_leaves_no_zip(
    pack_ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    signed = await signed_cycle(pack_ctx)
    run_id, olga = signed.run_id, signed.people["olga"]
    first = await pack.issue(pack_ctx, run_id, 1, who=olga)  # its sign-off stays approved through a purge
    await purge.hold(pack_ctx, TESTER, run_id, None)  # ds purge release
    # The issue's store transaction holds the cycle's row: a purge cannot start meanwhile.
    async with pack_ctx.pool.connection() as issuing, pack_ctx.pool.connection() as purging:
        await purge.refuse_purged(issuing, run_id, 1, lock=True)
        await purging.execute("SET lock_timeout = '100ms'")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            await repo.start_purge(purging, run_id, 1, "retention standard")
    judged = pack._judged

    async def purged_meanwhile(*args: Any, **kwargs: Any) -> Any:
        found = await judged(*args, **kwargs)
        async with pack_ctx.pool.connection() as conn:
            await conn.execute(
                "UPDATE ds.cycles SET ended_at = ended_at - interval '91 days' WHERE run_id = %s", (run_id,)
            )
        await purge.purge(pack_ctx, TESTER)  # the hourly pass, between the build and the store
        return found

    monkeypatch.setattr(pack, "_judged", purged_meanwhile)
    with pytest.raises(CyclePurged, match=re.escape(f"cycle 1 of run {run_id} was purged on ")):
        await pack.issue(pack_ctx, run_id, 1, who=olga)
    async with pack_ctx.pool.connection() as conn:
        packs = await repo.cycle_packs(conn, run_id, 1)
        cur = await conn.execute(
            "SELECT count(*) FROM ds.pack_zips JOIN ds.packs USING (pack_id) WHERE run_id = %s", (run_id,)
        )
        zips = await cur.fetchone()
    assert [(p["pack_id"], p["purged_at"] is not None) for p in packs] == [(first["pack_id"], True)]
    assert zips == (0,)
