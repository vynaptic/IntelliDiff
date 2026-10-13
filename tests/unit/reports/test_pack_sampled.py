"""A sampled cycle's evidence pack (docs/mvp/SAMPLING.md §11): its layout, the attested sample in facts and
manifest, the certificate's frame, draw, bounds, hand check, draws and warnings, the sampled claim verbatim,
the coverage rows of its strata, the audit segment's draw and close rows and the grant's later draws; P1 and
P2; and the raw-value gate over the new texts. Census packs keep their shape (test_pack_contents)."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest

from deltasleuth import approvals
from deltasleuth.context import Context
from deltasleuth.core import sampling
from deltasleuth.core.spec import compile_spec
from deltasleuth.core.types import CompiledSpec, Confirmation, KeyResult, Outcome, SideState
from deltasleuth.reports import pack
from deltasleuth.reports.csv import SAMPLE_COLUMNS
from deltasleuth.reports.render import Document
from deltasleuth.runner import sample
from deltasleuth.store import repo
from tests.unit.store import four_eyes
from tests.unit.store.four_eyes import COMPLETE, FRAME, counters, drawn, sampled

from .packs import CLOSE_NOTE, files_of, sampled_cycle, sign_off

pytestmark = pytest.mark.db
DOCS = Path(__file__).parents[3] / "docs" / "mvp"
LISTED = [
    "VERIFY.txt", "accuracy/attempts.json", "adjudication/attempts.json", "annex/ai_answers.json",
    "audit/chain.tsv", "certificate.pdf", "csv/coverage.csv", "csv/graph.csv", "csv/root_causes.csv",
    "facts/attests.json", "facts/plan.json", "facts/spec.json", "manifest.json",
]  # fmt: skip
SIDE_B = (
    "SIDE B IS NOT IN THE FRAME: keys only side b holds were not sampled; nothing here bounds extra records"
    " of side b"
)


def _documents(monkeypatch: pytest.MonkeyPatch) -> list[Document]:
    """The certificates the builder renders, kept."""
    documents: list[Document] = []
    render = pack.render_pdf

    def kept(document: Document) -> bytes:
        documents.append(document)
        return render(document)

    monkeypatch.setattr(pack, "render_pdf", kept)
    return documents


def _chain(files: dict[str, bytes]) -> list[tuple[int, str, dict[str, Any]]]:
    """audit/chain.tsv's rows: seq, object and the audit row."""
    rows = [line.split("\t", 5) for line in files["audit/chain.tsv"].decode().split("\n")[:-1]]
    return [(int(row[0]), row[4], json.loads(row[5])) for row in rows]


async def _failed(ctx: Context, run_id: str, version: CompiledSpec) -> None:
    """End the live sampled run's cycle with C001 found discrepant: its census stratum fails."""
    async with ctx.pool.connection() as conn:
        result = KeyResult("C001", Outcome.MISMATCH, COMPLETE, COMPLETE, confirmation=Confirmation.RECHECKED)
        await repo.save_key_result(conn, run_id, 1, "m-C001", result, [])
        await repo.set_counters(conn, run_id, counters(1, matched=5))
        found = await repo.get_draw(conn, run_id, 1)
        await repo.end_cycle(conn, run_id, sample=await sample.judge(conn, found, version))  # type: ignore[arg-type]
        await repo.set_run_status(conn, run_id, "completed")


async def _problems(ctx: Context, run_id: str) -> list[str]:
    async with ctx.pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
        assert run is not None
        return await pack.problems(ctx, conn, run, 1)


def test_the_sampled_claim_is_the_contract_s_verbatim() -> None:
    section = (DOCS / "SAMPLING.md").read_text().split("### 11.3")[1].split("### 11.4")[0]
    quoted = " ".join(section[section.index('"') + 1 : section.rindex('"')].split())
    assert pack.PROVES_SAMPLED.format(name="<name>", fp="<fp>", absent="<absent>") == quoted
    census = (DOCS / "PACK.md").read_text().split("### 3.4")[1].split("## 4.")[0]
    kept = " ".join(census[census.index('"') + 1 : census.rindex('"')].split())
    assert pack.PROVES.format(name="<name>", fp="<fp>") == kept  # census packs keep PACK §3.4's


async def test_a_sampled_pack_states_its_frame_draw_bounds_and_the_population_s_draws(
    pack_ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    documents = _documents(monkeypatch)
    signed = await sampled_cycle(pack_ctx, earlier=True, sign=False)
    olga, erin, frank = (signed.people[name] for name in ("olga", "erin", "frank"))
    async with pack_ctx.pool.connection() as conn:  # a later draw, live at the decision
        later_run, later = await drawn(
            conn, signed.spec_id, signed.spec, olga, end=False, approval_id=signed.spec_approval_id
        )
    signoff_id = await sign_off(pack_ctx, signed.run_id, 1, signed.spec, olga, erin)
    await _failed(pack_ctx, later_run, signed.spec)  # after the decision: the pack reads the grant's
    issued = await pack.issue(pack_ctx, signed.run_id, 1, who=olga)
    _, data = await pack.stored(pack_ctx, issued["pack_id"])
    files, signer = files_of(data), pack_ctx.signer
    assert signer is not None
    async with pack_ctx.pool.connection() as conn:
        signoff = await repo.get_approval(conn, signoff_id)
        spec_approval = await repo.get_approval(conn, signed.spec_approval_id)
        ledger = {d["draw_id"]: d for d in await repo.ledger(conn, ["table:db-a/customers"])}
    assert signoff is not None and spec_approval is not None
    earlier = next(d for d in ledger.values() if d["spec_name"] == "customer-profile-2")

    # The layout of a pack without an attested adjudication; the attests carry the sample (format 2 on).
    assert sorted(files) == sorted([*LISTED, *pack.UNLISTED])
    attests = json.loads(files["facts/attests.json"])
    assert attests == signoff["attests"] and attests["format"] == 3
    draw = attests["sample"]
    assert [draw[name] for name in ("draw_id", "method", "salt_label")] == [
        signed.draw_id,
        "bottom_k",
        "cutover-q4",
    ]
    assert [d["draw_id"] for d in draw["ledger"]] == [earlier["draw_id"]]
    salt = f"cutover-q4:{draw['nonce']}"
    assert draw["salt_sha256"] == hashlib.sha256(salt.encode()).hexdigest()

    # The manifest: the basis, the attested sample, the later draws at issue and the sampled claim.
    manifest = json.loads(files["manifest.json"])
    assert manifest["subject"]["basis"] == "sample" and manifest["claim"] == "observed"
    assert manifest["sample"] == draw and manifest["census"] == attests["census"]
    assert manifest["later_draws"] == [
        {"draw_id": later, "spec_name": "customer-profile", "run_id": later_run, "cycle": 1,
         "drawn_at": ledger[later]["drawn_at"].astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
         "status": "failed"}
    ]  # fmt: skip
    claim = pack.PROVES_SAMPLED.format(name="test-1", fp=signer.fingerprint, absent="0")
    assert manifest["claim_text"] == claim

    # The segment: the earlier draw's close, this draw's sample.draw row holding both SHA-256 values, and
    # the grant recording the later draw as the decision judged it.
    assert earlier["draw_id"] in manifest["audit"]["objects"]
    rows = _chain(files)
    [(close_seq, _, closed)] = [r for r in rows if r[2]["action"] == "sample.close"]
    assert closed["object"] == earlier["draw_id"] and closed["detail"]["note"] == CLOSE_NOTE
    assert close_seq == draw["ledger"][0]["close_seq"] < draw["draw_seq"]
    [row] = [r[2] for r in rows if r[0] == draw["draw_seq"]]
    assert (row["action"], row["object"]) == ("sample.draw", signed.run_id)
    assert (row["detail"]["salt_sha256"], row["detail"]["selection_sha256"]) == (
        draw["salt_sha256"],
        draw["selection_sha256"],
    )
    [(grant_seq, _, granted)] = [r for r in rows if r[1] == signoff_id and r[2]["action"] == "approval.grant"]
    assert granted["detail"]["later_draws"] == [{"draw_id": later, "status": "live"}]

    # The certificate.
    certificate = documents[-1]
    assert certificate.meta.startswith("Claim level: observed (sampled)\n")
    titled = {section.title: section for section in certificate.sections}
    assert [s.title for s in certificate.sections][2:5] == [
        "Frame (observed)", "Sample and bounds (observed)", "Draws of this population (observed)",
    ]  # fmt: skip
    assert "Census (observed)" not in titled
    assert titled["What the signature proves (observed)"].lines == [claim]
    at = f"{attests['census']['a']['at'][:16].replace('T', ' ')} UTC"
    assert titled["Frame (observed)"].lines == [
        f"Side a (legacy, target db-a): its population read to its end in 1 snapshot at {at} as the sampling"
        " frame; 6 row(s) of public.customers, 0 without a key",
        "Side b (new, target svc-b): not counted (a count of side b cannot show keys side b lost)",
        "Frame: 6 distinct keys (a only 6; b only 0; both 0); 0 outside key.pattern",
    ]
    bounds = titled["Sample and bounds (observed)"]
    moment, approved = pack._moment(draw["drawn_at"]), spec_approval["decided_at"].astimezone(UTC)
    assert bounds.lines == [
        f"Draw {signed.draw_id} by the installation at {moment}, in the transaction that stored the sample"
        f" (audit row {draw['draw_seq']}): salt cutover-q4 with nonce {draw['nonce']}, salt SHA-256"
        f" {draw['salt_sha256']}, selection SHA-256 {draw['selection_sha256']}; bottom_k; strata and"
        f" sizes approved in {signed.spec_approval_id} on {approved:%Y-%m-%d}",
        f"To check by hand: row {draw['draw_seq']} of audit/chain.tsv is action sample.draw and holds both"
        f" SHA-256 values, and the SHA-256 of the text {salt} is the salt's",
        "Keys are shown in clear here (key class business_code): with the nonce, anyone holding this pack can"
        " hash each key it lists and tell which of them lie in a stratum compared whole, a customer"
        " attribute.",
        "Contrast keys (compared, never bounded): 0, 0 compared, 0 discrepant.",
        "No bound is given for the strata pooled.",
    ]
    assert bounds.header[-2:] == ("Bound", "Verdict") and [tuple(r) for r in bounds.rows] == [
        (label, "standard", "0.1%", "census", 3, 3, 3, 0, 0, 0, 0, 0, "compared whole", "passed")
        for label in ("segment=retail", "segment=other")
    ]
    # The earlier draw's sample.draw row is its own run's, outside the segment: its number is not cited as
    # one of the pack's rows, its close row is.
    closer = earlier["draw_seq"]
    assert closer not in {r[0] for r in rows} and close_seq in {r[0] for r in rows}
    assert titled["Draws of this population (observed)"].lines == [
        f"Draw {earlier['draw_id']} (spec customer-profile-2, run {earlier['run_id']} cycle 1, audit row"
        f" {closer}, not in this pack), before this one: failed stratum segment=retail; closed by"
        f" {frank.actor} (audit row {close_seq}): {CLOSE_NOTE}",
        f"Later draws at the decision (audit row {grant_seq}): {later} live",
        f"Later draws at issue: {later} (spec customer-profile, run {later_run} cycle 1, drawn"
        f" {pack._moment(ledger[later]['drawn_at'])}): failed",
    ]
    assert certificate.warning == "\n".join([
        SIDE_B,
        "1 EARLIER DRAW OF THIS POPULATION DID NOT PASS (1 UNDER customer-profile-2): SEE DRAWS",
        "1 LATER DRAW WAS INCOMPLETE OR LIVE AT THE DECISION: SEE DRAWS",
        f"DRAW {later}, MADE AFTER THIS ONE, FAILED ON {ledger[later]['ended_at'].astimezone(UTC):%Y-%m-%d}:"
        " SEE DRAWS",
    ])  # fmt: skip

    # coverage.csv: a row per sign-off stratum, then the contrast keys, the sample columns appended.
    coverage = files["csv/coverage.csv"].decode()
    header = next(csv.reader(io.StringIO(coverage)))
    assert header[-len(SAMPLE_COLUMNS) :] == list(SAMPLE_COLUMNS)
    found = list(csv.DictReader(io.StringIO(coverage)))
    assert [r["stratum"] for r in found] == ["*", "segment=retail", "segment=other", "(contrast)"]
    assert [(r["tier"], r["target"], r["basis"], r["drawn"], r["verdict"]) for r in found[1:3]] == [
        ("standard", "0.001", "census", "3", "passed")
    ] * 2
    # No sampled or frame key, and no per-key label beside a key, enters the pack (as a word of its own:
    # a generated id may hold a key's characters by chance).
    words = {key: re.compile(rb"(?<![0-9A-Za-z])" + key.encode() + rb"(?![0-9A-Za-z])") for key, _ in FRAME}
    assert not [(key, n) for key, word in words.items() for n, d in files.items() if word.search(d)]


async def test_a_failed_sampled_cycle_s_pack_is_built_to_judge_it_and_warns_of_absent_keys(
    pack_ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two keys absent on both sides of a draw whose strata are compared whole: the cycle fails (G8), yet its
    pack is built to judge it and says so; a later draw still live at issue is listed as it stands."""
    documents = _documents(monkeypatch)
    signed = await sampled_cycle(pack_ctx, absent=["C002", "C005"])
    async with pack_ctx.pool.connection() as conn:
        later_run, later = await drawn(conn, signed.spec_id, signed.spec, signed.people["olga"], end=False)
    at = f"cycle 1 of run {signed.run_id}"
    problems = await _problems(pack_ctx, signed.run_id)
    assert [p for p in problems if "absent on both sides" in p] == [
        f"stratum segment={label} of {at} has 1 key(s) absent on both sides, over the 0 its 0.1% target"
        " allows of 3 drawn: the frame was stale; draw again"
        for label in ("retail", "other")
    ]
    certificate = documents[-1]
    assert certificate.warning == "\n".join([
        SIDE_B, "2 FRAME KEYS WERE ABSENT ON BOTH SIDES: NOT JUDGED, OUTSIDE EVERY BOUND BUT THE WORST CASE",
    ])  # fmt: skip
    titled = {section.title: section for section in certificate.sections}
    claimed = titled["What the signature proves (observed)"].lines[0]
    assert " counted as discrepant. 2 frame keys found absent on both sides were not judged" in claimed
    assert titled["Draws of this population (observed)"].lines[-1].startswith(
        f"Later draws at issue: {later} (spec customer-profile, run {later_run} cycle 1, drawn "
    ) and titled["Draws of this population (observed)"].lines[-1].endswith("): live")


async def test_p1_a_frame_that_does_not_enumerate_side_a_is_not_a_census(pack_ctx: Context) -> None:
    raw = sampled().raw
    raw["key"]["source"]["source"] = {
        "kind": "paginated_endpoint", "target": "svc-b", "request": {"method": "GET", "path": "/list"},
        "pagination": {"kind": "cursor", "itemsPath": "$.items", "cursorPath": "$.next", "cursorParam": "c"},
        "keyPath": "$.id", "strataFields": {"segment": "$.segment"},
    }  # fmt: skip
    listed = {"basis": "listing", "target": "svc-b", "items": 6, "keys": 6, "unfit": 0,
              "first_at": "2026-09-30T08:00:00.0Z", "last_at": "2026-09-30T08:00:01.0Z"}  # fmt: skip
    signed = await sampled_cycle(pack_ctx, spec=compile_spec(raw), census={"b": listed})
    assert await _problems(pack_ctx, signed.run_id) == [
        f"cycle 1 of run {signed.run_id} is not a census: its sampling frame does not enumerate side a (the"
        " baseline); a frame of side b cannot show keys side b lost"
    ]


@pytest.mark.parametrize("member", ["salt_sha256", "selection_sha256", "draw_id"])
async def test_p2_a_draw_its_sample_draw_row_does_not_match_gets_no_pack(
    pack_ctx: Context, member: str
) -> None:
    """The row the worker audited names another salt (a nonce that does not hash to it), another selection
    or another draw: the stored draw is not the one committed before any key was compared."""
    other = {
        "salt_sha256": hashlib.sha256(f"cutover-q4:{sampling.nonce()}".encode()).hexdigest(),
        "selection_sha256": "0" * 64,
        "draw_id": repo.new_draw_id(),
    }
    signed = await sampled_cycle(pack_ctx, audited={member: other[member]})
    unmatched = (
        f"the sample {signed.draw_id} of cycle 1 of run {signed.run_id} does not match its sample.draw"
    )
    unordered = [f"{unmatched} audit row: its order in the ledger is unknown"] if member == "draw_id" else []
    assert await _problems(pack_ctx, signed.run_id) == [*unordered, f"{unmatched} audit row: no pack"]


async def test_a_cycle_that_ended_incomplete_gets_no_pack(pack_ctx: Context) -> None:
    """C001 waited for its retry when the cycle ended: it holds a result, so nothing was cancelled or owed,
    yet the stored judgement is incomplete, and a sign-off made of it anyway gets no pack (R-b)."""
    signed = await sampled_cycle(pack_ctx, end=False, sign=False)
    async with pack_ctx.pool.connection() as conn:
        waiting = KeyResult("C001", Outcome.INCONCLUSIVE, SideState.error("timeout"), COMPLETE,
                            inconclusive_reason="timeout")  # fmt: skip
        await repo.save_key_result(conn, signed.run_id, 1, "m-C001", waiting, [])
        await conn.execute(
            "UPDATE ds.run_keys SET state = 'pending', retries = 1"
            " WHERE run_id = %s AND key_masked = 'm-C001'",
            (signed.run_id,),
        )
        await repo.set_counters(conn, signed.run_id, counters(1, matched=5))
        run = await repo.get_run(conn, signed.run_id)
        _, judgement = await sample.ending(pack_ctx, conn, run)  # type: ignore[arg-type]
        await repo.end_cycle(conn, signed.run_id, sample=judgement)
        await repo.set_run_status(conn, signed.run_id, "completed")
    await sign_off(pack_ctx, signed.run_id, 1, signed.spec, signed.people["olga"], signed.people["erin"])
    at = f"cycle 1 of run {signed.run_id}"
    assert await _problems(pack_ctx, signed.run_id) == [
        f"stratum segment=retail of {at} is over its 0.1% target: compared whole, its 3 keys allow 0"
        " discrepant or unresolved key(s); it has 1",
        f"the sample {signed.draw_id} of {at} ended incomplete (stratum segment=retail unfinished): its cycle"
        " ended before every key was final; draw again",
    ]


async def test_p2_a_draw_no_sample_draw_row_names_is_refused_beside_another_draw(pack_ctx: Context) -> None:
    """With an earlier draw on the ledger, a draw whose row names another draw has no place to compare:
    its sign-off is refused (its order unknown) and its pack gets P2, never an error."""
    signed = await sampled_cycle(pack_ctx, earlier=True, sign=False, audited={"draw_id": repo.new_draw_id()})
    unmatched = (
        f"the sample {signed.draw_id} of cycle 1 of run {signed.run_id} does not match its sample.draw"
    )
    async with pack_ctx.pool.connection() as conn:
        run = await repo.get_run(conn, signed.run_id)
        assert run is not None
        found = await approvals.signoff_problems(conn, run, 1, signed.spec)
        facts = await approvals.attests(conn, run, 1, signed.spec)
        later = await sample.later_draws(conn, signed.run_id, 1)
    assert found == [f"{unmatched} audit row: its order in the ledger is unknown"]
    assert (facts["sample"]["draw_seq"], facts["sample"]["ledger"], later) == (None, [], [])
    await sign_off(pack_ctx, signed.run_id, 1, signed.spec, signed.people["olga"], signed.people["erin"])
    assert await _problems(pack_ctx, signed.run_id) == [
        f"{unmatched} audit row: its order in the ledger is unknown",
        f"{unmatched} audit row: no pack",
    ]


async def test_the_raw_value_gate_reads_the_sampled_texts(pack_ctx: Context) -> None:
    """Under keys of class identifier (^C[0-9]{3}$), a sampled pack's own texts (draw ids, the nonce, SHA-256
    values, labels, the population, a close note) hold no key; a bucket label shaped like a key refuses it,
    the spec's place named first."""
    signed = await sampled_cycle(pack_ctx, spec=sampled(key="identifier"), earlier=True)
    assert await _problems(pack_ctx, signed.run_id) == []
    issued = await pack.issue(pack_ctx, signed.run_id, 1, who=signed.people["olga"])
    _, data = await pack.stored(pack_ctx, issued["pack_id"])
    files = files_of(data)
    searched = pack.raw_values(signed.spec, files)
    assert searched == [] and b"segment=retail" in files["csv/coverage.csv"]
    assert json.loads(files["manifest.json"])["sample"]["nonce"] in files["facts/attests.json"].decode()

    raw = sampled("customer-profile-3", key="identifier").raw
    raw["key"]["source"]["strata"][0]["buckets"][0]["label"] = "C900"
    shaped = await sampled_cycle(pack_ctx, spec=compile_spec(raw))
    found = await _problems(pack_ctx, shaped.run_id)
    assert found[-1].startswith("the pack's texts hold ") and found[-1].endswith(
        " raw key-shaped value(s) (key.source.strata[0].buckets[0].label): no pack carries them"
    )


async def test_the_raw_value_gate_reads_a_bucket_s_numbers(
    pack_ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A watch-listed account (^[0-9]{8}$ keys of class identifier) pinned into a stratum as a JSON number,
    as a value and as a range, is searched as its text: no pack carries it."""
    accounts = [(str(40012340 + n), segment) for n, (_, segment) in enumerate(FRAME, 1)]
    monkeypatch.setattr(four_eyes, "FRAME", accounts)
    raw = sampled(key="identifier").raw
    raw["key"]["pattern"] = "^[0-9]{8}$"
    raw["key"]["source"]["strata"].append({"name": "watch", "cel": "int(key)", "buckets": [
        {"label": "listed", "values": [40012345]}, {"label": "pinned", "min": 40012346, "max": 40012346},
        {"label": "rest"}]})  # fmt: skip
    signed = await sampled_cycle(pack_ctx, spec=compile_spec(raw))
    found = await _problems(pack_ctx, signed.run_id)
    assert found == [
        "the pack's texts hold 3 raw key-shaped value(s) (key.source.strata[1].buckets[0].values[0]): no pack"
        " carries them"
    ]
    files = {"facts/spec.json": pack._spec_text(raw).encode()}  # its members sorted, as the pack writes it
    assert pack.raw_values(signed.spec, files) == [
        "key.source.strata[1].buckets[0].values[0]",
        "key.source.strata[1].buckets[1].max",
        "key.source.strata[1].buckets[1].min",
    ]
