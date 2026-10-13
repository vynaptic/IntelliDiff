"""What the reports say of the licence: report.pdf's title block states the licence
a run was made under, and the P0 summary's warning an evaluation's or a grace's; the plan a pack hashes
records it without the customer; the raw-value gate reads the licence's ids as DeltaSleuth's and skips its
fixed words, and a soak paused for the licence keeps its packs under an integer key pattern."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from deltasleuth import approvals, licence
from deltasleuth.auth.principal import WORKER
from deltasleuth.context import Context
from deltasleuth.reports import pack
from deltasleuth.reports.data import load_run
from deltasleuth.reports.pdf import report_document
from deltasleuth.reports.summary import summary_document
from deltasleuth.runner import runs
from deltasleuth.runner.cycle import finish_cycle
from deltasleuth.security import rawvalues
from deltasleuth.store import repo
from tests.conftest import TESTER, licensed, unlicensed

from .conftest import doc, seed_run, unit_spec
from .packs import listed_spec, samples, sign_off, signed_cycle

pytestmark = pytest.mark.usefixtures("vendor_keys")  # a vendor build
LICENCE_ID = "LI-01JB8Q7ZK3M9W2XR5T4VNH6D0C"
MEMBER = {
    "licence_id": LICENCE_ID, "customer": "Example Bank holdings plc", "order": "ORD-2026-014",
    "installation_id": "I-3f2a9c1b7d4e5a60", "tier": "enterprise", "pairs": None, "starts": "2026-10-01",
    "ends": "2027-09-30", "state": "grace",
    "key": "077646502f1985bea359f0983aa09b8a446d3d5d9a7d873df55a6006009665c4",
}  # fmt: skip


async def _recorded(ctx: Context, record: dict[str, Any] | None) -> str:
    """A completed run whose params record ``record`` as the licence it was made under."""
    run_id = await seed_run(ctx, {"K001": (doc("K001"), doc("K001"))})
    if record is not None:
        async with ctx.pool.connection() as conn:
            await conn.execute(
                "UPDATE ds.runs SET params = params || jsonb_build_object('licence', %s::jsonb)"
                " WHERE run_id = %s",
                (json.dumps(record), run_id),
            )
    return run_id


@pytest.mark.db
async def test_licence_labels(ctx: Context, tmp_path: Path) -> None:
    evaluation = {"licence_id": None, "tier": None, "state": "evaluation", "ends": None}
    grace = {"licence_id": LICENCE_ID, "tier": "pro", "state": "grace", "ends": "2027-09-30"}
    valid = {"licence_id": LICENCE_ID, "tier": "enterprise", "state": "valid", "ends": "2027-09-30"}
    behind = valid | {"clock_behind": True}
    said = {}
    for name, record in (("evaluation", evaluation), ("grace", grace), ("valid", valid), ("behind", behind),
                         ("none", None)):  # fmt: skip
        run_id = await _recorded(ctx, record)
        title = (await report_document(ctx, run_id)).meta.splitlines()
        summary = summary_document(await load_run(ctx, run_id), tmp_path)
        said[name] = (run_id, title, summary.warning)
    run_id, title, warning = said["evaluation"]
    label = (
        f"Unlicensed evaluation: run {run_id} was made without a licence. Not for production use; no evidence"
        " pack is issued for it."
    )
    assert title[title.index(label) - 1].startswith("Unapproved spec version")  # after the approval lines
    assert warning == label
    run_id, title, warning = said["grace"]
    label = f"Run {run_id} was made in the 30-day grace of licence {LICENCE_ID}, which ended on 2027-09-30."
    assert label in title and warning == label
    run_id, title, warning = said["valid"]
    assert f"Run {run_id} was made under licence {LICENCE_ID} (enterprise)." in title and warning is None
    run_id, title, warning = said["behind"]
    assert (
        f"Run {run_id} was made under licence {LICENCE_ID} (enterprise). The database clock was behind the"
        " newest run when it was made."
    ) in title
    run_id, title, warning = said["none"]  # made before 010, or by an older release
    assert not any("licence" in line.lower() for line in title) and warning is None


@pytest.mark.db
async def test_plan_record_without_customer(ctx: Context) -> None:
    found = await licensed(ctx)  # Enterprise, from today
    async with ctx.pool.connection() as conn:
        spec_id = await repo.save_spec(conn, unit_spec(), created_by=TESTER.actor)
    run_id = await runs.create_run(ctx, spec_id, "batch", keys=["K001"], who=TESTER)
    async with ctx.pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
        cur = await conn.execute(
            "SELECT detail FROM ds.audit_log WHERE action = 'create_run' AND object = %s", (run_id,)
        )
        (created,) = await cur.fetchone()  # type: ignore[misc]
    assert run is not None
    record = {
        "licence_id": found.licence_id,
        "tier": "enterprise",
        "state": "valid",
        "ends": found.ends.isoformat(),
    }
    plan = approvals.plan(run["params"])
    assert json.loads(plan)["licence"] == created["licence"] == record
    assert created["plan_sha256"] == hashlib.sha256(plan.encode()).hexdigest()  # facts/plan.json holds it
    assert found.customer not in plan and found.order not in plan and "customer" not in plan


def test_raw_value_gate_licence_texts() -> None:
    key = listed_spec(pii=True).raw["key"] | {"pattern": "^[a-z0-9]{4,30}$"}  # lowercase words, hex runs
    spec = listed_spec(pii=True, key=key, knownDiffs=[])
    for word in (
        LICENCE_ID,
        MEMBER["installation_id"],
        MEMBER["key"],
        "2027-09-30",
        "user:U-3f2a9c1b7d4e5a60",
    ):
        assert not rawvalues.key_shaped(spec, word), word
    # Not an id DeltaSleuth writes: its hex part is a key; nor is a word.
    assert rawvalues.key_shaped(spec, "Z-3f2a9c1b7d4e5a60") and rawvalues.key_shaped(spec, "enterprise")

    def found(manifest: dict[str, Any]) -> list[str]:
        return pack.raw_values(spec, {"manifest.json": json.dumps(manifest).encode()})

    assert found({"licence": MEMBER}) == []  # its words are fixed text, its ids DeltaSleuth's
    assert sorted(found({"other": MEMBER})) == [
        "manifest.json other.customer", "manifest.json other.order", "manifest.json other.state",
        "manifest.json other.tier",
    ]  # fmt: skip


INTEGERS = ["^[0-9]+$", "^[1-9][0-9]{0,5}$"]  # identifiers, and examples/public/rickandmorty's


def _paused_row(detail: dict[str, Any]) -> bytes:
    """audit/chain.tsv's line of a ``paused`` row with ``detail``, as a pack carries it."""
    text = json.dumps({"actor": "system:worker", "action": "paused", "object": "R-X", "detail": detail})
    return f"12\tprev\tobjprev\thash\tR-X\t{text}\n".encode()


@pytest.mark.parametrize("pattern", INTEGERS)
def test_licence_pauses_hold_no_raw_value(pattern: str) -> None:
    key = listed_spec(pii=True).raw["key"] | {"pattern": pattern}
    spec = listed_spec(pii=True, key=key, knownDiffs=[])
    now, starts, ends = datetime(2027, 11, 5, tzinfo=UTC), date(2026, 10, 1), date(2027, 9, 30)
    pro = licence.Licence(LICENCE_ID, "Example Bank plc", "ORD-2026-014", MEMBER["installation_id"], "pro", 5,
                          starts, ends, now, MEMBER["key"], b"", "")  # fmt: skip
    counted = tuple({"name": name} for name in ("a1", "b22", "c333", "d4444", "e55555", "cards-7"))
    under_sso = {"sso": True, "oidc": True, "packs": True}
    standings = {
        "unseated": licence.Standing(pro.installation_id, now, "valid", pro, counted=counted),
        "expired": licence.Standing(pro.installation_id, now, "expired", pro),
        "sso": licence.Standing(pro.installation_id, now, "evaluation", None, **under_sso),
    }
    for code, standing in standings.items():
        message, detail = standing.paused("cards-7")  # type: ignore[misc]
        # Why the row records no message: its numbers read as keys. The SSO refusal holds no number.
        assert rawvalues.key_shaped(spec, message) == (code != "sso"), code
        row = {"reason": licence.LICENCE_PAUSE, "approval_id": None, "licence": detail}
        assert pack.raw_values(spec, {"audit/chain.tsv": _paused_row(row)}) == [], (code, detail)
    assert standings["unseated"].paused("cards-7")[1] == {  # type: ignore[index]
        "licence_id": LICENCE_ID, "tier": "pro", "state": "valid", "ends": "2027-09-30",
        "refusal": "unseated", "count": 6, "limit": 5, "since": "2026-10-01",
    }  # fmt: skip


@pytest.mark.db
@pytest.mark.parametrize("pattern", INTEGERS)
async def test_a_soak_paused_for_the_licence_keeps_its_packs(pack_ctx: Context, pattern: str) -> None:
    """The boundary pauses a soak whose licence and grace ended; its cycle is signed off, the
    renewal installed and the soak resumed: the pack's raw-value gate finds nothing in its rows."""
    key = listed_spec(pii=True).raw["key"] | {"pattern": pattern}
    spec = listed_spec(pii=True, key=key, knownDiffs=[], execution={"cycleInterval": "0s"})
    soak = await signed_cycle(pack_ctx, spec=spec, pairs=samples(pii=True), sign=False, mode="soak")
    ended = await unlicensed(pack_ctx, ended="enterprise")
    await finish_cycle(pack_ctx, soak.run_id, who=WORKER)
    await sign_off(pack_ctx, soak.run_id, 1, spec, soak.people["olga"], soak.people["erin"])
    await licensed(pack_ctx)  # the renewal, from today
    assert await runs.resume_run(pack_ctx, soak.run_id, who=TESTER) == "running"
    async with pack_ctx.pool.connection() as conn:
        run = await repo.get_run(conn, soak.run_id)
        assert run is not None and (run["cycle"], run["status"]) == (2, "running")
        cur = await conn.execute(
            "SELECT detail FROM ds.audit_log WHERE action = 'paused' AND object = %s", (soak.run_id,)
        )
        (paused,) = await cur.fetchone()  # type: ignore[misc]
        problems = await pack.problems(pack_ctx, conn, run, 1)
    assert ended is not None and paused["licence"] == {
        "licence_id": ended.licence_id, "tier": "enterprise", "state": "expired", "ends": str(ended.ends),
        "refusal": "expired",
    }  # fmt: skip
    assert problems == []
