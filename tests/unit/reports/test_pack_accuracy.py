"""Labelled accuracy in an evidence pack (docs/mvp/ACCURACY.md §9): the attested labelling's labels without
keys and its score, hashed as the sign-off attests and its ``accuracy.label`` row holds; every labelling of
the cycle, keyless; the manifest; the certificate's section (``lines``, ``LIMITS`` verbatim, the hand check)
and each warning; a labelling that no longer hashes gets no pack; a sign-off requested before 013 packs as
before."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from deltasleuth import approvals
from deltasleuth.context import Context
from deltasleuth.reports import accuracy, pack
from deltasleuth.reports.csv import parse_csv
from deltasleuth.reports.data import accuracy_section, load_run
from deltasleuth.reports.render import Document
from deltasleuth.store import repo
from tests.unit.store.four_eyes import token

from .packs import (
    TRUTH,
    files_of,
    labelled_cycle,
    labelled_pairs,
    labelled_sheet,
    listed_spec,
    sign_off,
    signed_cycle,
    tools,
    unzip,
)

pytestmark = pytest.mark.db
SECTION = "Labelled accuracy (observed)"
NOT_REVIEWED = {"1": ("UNDECIDABLE", "", "NOT_REVIEWED"), "2": ("UNDECIDABLE", "", "NO_TRUTH")}


@pytest.fixture
def documents(monkeypatch: pytest.MonkeyPatch) -> list[Document]:
    """Every document the pack builder renders: the last is the issued pack's certificate."""
    found: list[Document] = []
    render = pack.render_pdf

    def kept(document: Document) -> bytes:
        found.append(document)
        return render(document)

    monkeypatch.setattr(pack, "render_pdf", kept)
    return found


async def _issue(ctx: Context, signed: Any) -> tuple[dict[str, bytes], bytes]:
    issued = await pack.issue(ctx, signed.run_id, 1, who=signed.people["olga"])
    _, data = await pack.stored(ctx, issued["pack_id"])
    return files_of(data), data


async def test_a_pack_carries_the_attested_labelling_without_keys(
    pack_ctx: Context, documents: list[Document], tmp_path: Path
) -> None:
    signed, row = await labelled_cycle(pack_ctx, blank=True)
    files, data = await _issue(pack_ctx, signed)
    async with pack_ctx.pool.connection() as conn:
        signoff = await repo.get_approval(conn, signed.signoff_id)  # type: ignore[arg-type]
        blank, _ = sorted(await repo.list_labellings(conn, signed.run_id, 1), key=lambda v: v["labelling_id"])
        label = await repo.last_audit(conn, "accuracy.label", obj=row["labelling_id"])
    assert signoff is not None and label is not None
    attested, manifest = signoff["attests"]["accuracy"], json.loads(files["manifest.json"])

    def sha(name: str) -> str:
        return hashlib.sha256(files[name]).hexdigest()

    # The files hash to what the sign-off attests and the labelling's audit row holds.
    assert sha("accuracy/labels.csv") == attested["labels_sha256"] == label["detail"]["labels_sha256"]
    assert sha("accuracy/score.json") == attested["score_sha256"] == label["detail"]["score_sha256"]
    assert files["accuracy/labels.csv"].decode() == accuracy.keyless(row["sheet"])
    assert json.loads(files["accuracy/score.json"]) == row["accuracy"]
    assert json.loads(files["facts/attests.json"])["accuracy"] == attested
    assert attested["labelling_id"] == row["labelling_id"]
    assert signoff["attests"]["labellings"] == {"drawn": 2, "labelled": 1}
    assert manifest["accuracy"] == {"attested": True, "draws": 2, "labelled": 1}
    assert {blank["labelling_id"], row["labelling_id"]} <= set(manifest["audit"]["objects"])
    assert all(manifest["files"][name] == sha(name) for name in files if name.startswith("accuracy/"))
    # Every labelling of the cycle, labelled or not; no file holds a key, masked or raw, or a note.
    unlabelled, kept = json.loads(files["accuracy/attempts.json"])
    assert (unlabelled["labelling_id"], unlabelled["labelled_at"], unlabelled["labels"]) == (
        blank["labelling_id"], None, None,
    )  # fmt: skip
    names = ("labelling_id", "salt", "selection_sha256", "sheet_sha256", "labels_sha256", "score_sha256")
    assert {name: kept[name] for name in names} == {name: row[name] for name in names}
    assert "upload_sha256" not in kept  # no hash of the upload: it would be one of the notes
    assert (kept["drawn_by_name"], kept["labelled_by_name"], kept["truth_source"], kept["truth_as_of"]) == (
        "olga", "frank", "CBS-PROD.account_master", "2026-10-01",
    )  # fmt: skip
    assert kept["labels"]["discrepant"]["B_RIGHT"] == 5 and kept["labels"]["accepted"] is None
    assert (kept["size"], kept["agreed"], kept["counts"]) == (5, 4, row["selection"]["counts"])
    for name in ("accuracy/labels.csv", "accuracy/score.json", "accuracy/attempts.json"):
        text = files[name].decode()
        assert not re.search(r"K0[0-9]{2}|O-[0-9a-f]{16}|called the branch", text), name
    assert "K0" in row["sheet"]  # the stored sheet holds the masked keys; the pack does not
    # The certificate: what the labelling says, what it may not be read as, and how to check it by hand.
    section = next(s for s in documents[-1].sections if s.title == SECTION)
    rows = [line.split("\t", 5) for line in files["audit/chain.tsv"].decode().split("\n")[:-1]]
    chained = next(r for r in rows if r[4] == row["labelling_id"] and '"action": "accuracy.label"' in r[5])
    seq = chained[0]
    assert section.lines == [
        *accuracy.lines(row, signoff=signed.signoff_id),
        accuracy.LIMITS,
        "To check by hand: sha256sum accuracy/labels.csv accuracy/score.json prints facts/attests.json's"
        f" accuracy.labels_sha256 and accuracy.score_sha256; row {seq} of audit/chain.tsv is the"
        f" accuracy.label row of {row['labelling_id']} holding both",
    ]
    assert section.lines[0].endswith(f", attested by sign-off {signed.signoff_id}.")
    assert documents[-1].warning == (
        "2 LABELLINGS OF THIS CYCLE WERE DRAWN (1 LABELLED); THE NEWEST A PERSON LABELLED IS ATTESTED"
    )
    tools()  # the hand check, with stock tools
    folder = unzip(data, tmp_path / "pack")
    summed = subprocess.run(
        ["sh", "-c", "sha256sum accuracy/labels.csv accuracy/score.json"], cwd=folder, capture_output=True,
        text=True, check=True,
    )  # fmt: skip
    assert [line.split()[0] for line in summed.stdout.splitlines()] == [
        attested["labels_sha256"], attested["score_sha256"],
    ]  # fmt: skip
    held = json.loads(chained[5])
    assert (held["action"], held["object"]) == ("accuracy.label", row["labelling_id"])
    assert (held["detail"]["labels_sha256"], held["detail"]["score_sha256"]) == (
        attested["labels_sha256"], attested["score_sha256"],
    )  # fmt: skip


async def test_the_pack_s_labels_have_no_item_number_and_are_not_in_the_sheet_s_order(
    pack_ctx: Context,
) -> None:
    """The sheet's rows run in the order of their keys' salted digests, and the pack holds the salt: labels
    listed in that order, or numbered as the selection's hashed lines number them, could be put back on
    the masked keys a pack reader holds (its key.view rows). The pack's labels are sorted by their own
    cells."""
    labels = {
        "1": ("A_RIGHT", "SOURCE_DOCUMENT", ""),
        "3": ("BOTH_WRONG", "OWNER_CONFIRMED", ""),
        "4": ("BOTH_RIGHT", "BUSINESS_RULE", ""),
        "5": ("UNDECIDABLE", "", "NO_TRUTH"),
    }
    signed, row = await labelled_cycle(pack_ctx, labels=labels)
    files, _ = await _issue(pack_ctx, signed)
    header, rows = parse_csv(files["accuracy/labels.csv"])
    kept = parse_csv(row["sheet"].encode())[1]
    assert header == list(accuracy.KEYLESS_COLUMNS) and "item" not in header and "key" not in header
    cells = [[r[name] for name in header] for r in rows]
    assert cells == sorted(cells) and len(rows) == len(kept) == 9
    in_the_sheet = [r["label"] for r in kept if r["population"] == "discrepant"]  # by digest: items 1 to 5
    in_the_pack = [r["label"] for r in rows if r["population"] == "discrepant"]
    assert in_the_sheet == ["A_RIGHT", "B_RIGHT", "BOTH_WRONG", "BOTH_RIGHT", "UNDECIDABLE"]
    assert in_the_pack == ["A_RIGHT", "BOTH_RIGHT", "BOTH_WRONG", "B_RIGHT", "UNDECIDABLE"]
    # The score re-derived from the pack's labels is the attested one: their order carries nothing.
    selection, truth = row["selection"], row["accuracy"]["truth"]
    assert accuracy.tally(rows, selection, truth) == json.loads(files["accuracy/score.json"])


async def test_a_labelling_no_person_labelled_is_counted_on_the_certificate_not_called_unmeasured(
    pack_ctx: Context, documents: list[Document]
) -> None:
    """A labelling uploaded through a token is attested by no sign-off. The certificate then says so with
    the cycle's labellings counted, warns of it and points to accuracy/attempts.json, and the sign-off
    attests the counts: it never says nothing was measured."""
    signed = await signed_cycle(pack_ctx, sign=False, pairs=labelled_pairs())
    olga, frank, erin = (signed.people[name] for name in ("olga", "frank", "erin"))
    async with pack_ctx.pool.connection() as conn:
        bot = await token(conn, "ci-bot", frank)
    drawn, _ = await accuracy.store_draw(
        pack_ctx, signed.run_id, 1, who=olga, truth=TRUTH, size=5, per_cause=0
    )
    adverse = {str(item): ("A_RIGHT", "SYSTEM_OF_RECORD", "") for item in range(1, 6)}
    upload = labelled_sheet(drawn["sheet"], adverse)
    await accuracy.store_labels(pack_ctx, drawn["labelling_id"], upload, who=bot)
    signoff_id = await sign_off(pack_ctx, signed.run_id, 1, signed.spec, olga, erin)
    files, _ = await _issue(pack_ctx, replace(signed, signoff_id=signoff_id))
    attests = json.loads(files["facts/attests.json"])
    assert (attests["format"], attests["accuracy"], attests["labellings"]) == (
        3,
        None,
        {"drawn": 1, "labelled": 1},
    )
    assert json.loads(files["manifest.json"])["accuracy"] == {"attested": False, "draws": 1, "labelled": 1}
    section = next(s for s in documents[-1].sections if s.title == SECTION)
    assert section.lines == [
        "No labelled accuracy is attested: 1 labelling of this cycle was drawn (1 labelled), none by a"
        " signed-in person; accuracy/attempts.json lists each with its label counts."
    ]
    assert documents[-1].warning == (
        "1 LABELLING OF THIS CYCLE WAS DRAWN (1 LABELLED); NONE WAS LABELLED BY A SIGNED-IN PERSON AND NONE"
        " IS ATTESTED"
    )
    (attempt,) = json.loads(files["accuracy/attempts.json"])
    assert (attempt["labelled_by"], attempt["labels"]["discrepant"]["A_RIGHT"]) == (bot.actor, 5)
    assert "accuracy/labels.csv" not in files and "accuracy/score.json" not in files
    # The reports of the cycle state that labelling, as labelled by no signed-in person.
    said = accuracy_section(await load_run(pack_ctx, signed.run_id, 1)).lines  # type: ignore[union-attr]
    assert said[0].endswith(", not by a signed-in person: no sign-off attests it.")
    assert said[3].startswith("Truth favoured b (new) alone: 0 of 5 ") and said[-1] == accuracy.LIMITS


async def test_a_cycle_whose_labellings_are_unlabelled_was_not_measured(
    pack_ctx: Context, documents: list[Document]
) -> None:
    signed = await signed_cycle(pack_ctx, sign=False, pairs=labelled_pairs())
    olga, erin = signed.people["olga"], signed.people["erin"]
    for size in (2, 3):
        await accuracy.store_draw(pack_ctx, signed.run_id, 1, who=olga, truth=TRUTH, size=size, per_cause=0)
    signoff_id = await sign_off(pack_ctx, signed.run_id, 1, signed.spec, olga, erin)
    files, _ = await _issue(pack_ctx, replace(signed, signoff_id=signoff_id))
    assert json.loads(files["facts/attests.json"])["labellings"] == {"drawn": 2, "labelled": 0}
    section = next(s for s in documents[-1].sections if s.title == SECTION)
    assert section.lines == ["No labelled accuracy is attested: which side is right was not measured."]
    assert documents[-1].warning is None
    said = accuracy_section(await load_run(pack_ctx, signed.run_id, 1)).lines  # type: ignore[union-attr]
    assert said == ["Not measured: no labelled sample of this cycle. 2 labellings were drawn, none labelled."]


async def test_the_certificate_warns_of_a_labeller_of_the_run_undecided_items_and_a_circular_truth(
    pack_ctx: Context, documents: list[Document]
) -> None:
    truth = {"source": "svc-a.accounts", "as_of": "2026-10-01"}
    signed, _ = await labelled_cycle(pack_ctx, by="olga", truth=truth, labels=NOT_REVIEWED)
    files, _ = await _issue(pack_ctx, signed)
    attested = json.loads(files["facts/attests.json"])["accuracy"]
    assert attested["labeller"] == {"started_run": True, "spec_author": False}
    assert attested["discrepant"]["reasons"] == {"NO_TRUTH": 1, "NOT_REVIEWED": 1}
    assert documents[-1].warning == "\n".join([
        f"THE LABELLER STARTED RUN {signed.run_id}: THE LABELS ARE NOT INDEPENDENT OF THE RUN",
        "2 OF 5 SAMPLED DISCREPANCIES ARE UNDECIDABLE, 1 OF THEM NOT REVIEWED",
        "THE TRUTH SOURCE NAMES SIDE A (LEGACY): LABELS RESTING ON A COMPARED SIDE ARE CIRCULAR AND MEASURE"
        " NOTHING",
    ])  # fmt: skip
    section = next(s for s in documents[-1].sections if s.title == SECTION)
    assert section.lines[1:3] == [
        "Warning: the truth source names side a (legacy): labels resting on a compared side are circular and"
        " measure nothing.",
        f"Warning: the labeller started run {signed.run_id}: the labels are not independent of the run.",
    ]
    assert accuracy.LIMITS in section.lines


async def test_the_certificate_warns_of_a_spec_author_s_labels_and_of_records_not_locatable(
    pack_ctx: Context, documents: list[Document]
) -> None:
    security = {"unlisted": "tokenize", "tokenize": [{"path": "$.name", "class": "name"}]}
    spec = listed_spec(pii=True, security=security)  # a tokenised key and nothing in clear
    signed, row = await labelled_cycle(pack_ctx, by="bob", spec=spec, pairs=labelled_pairs(pii=True))
    files, _ = await _issue(pack_ctx, signed)
    attested = json.loads(files["facts/attests.json"])["accuracy"]
    assert (attested["labeller"], attested["unlocatable"]) == (
        {"started_run": False, "spec_author": True},
        True,
    )
    independent = ": THE LABELS ARE NOT INDEPENDENT OF THE RUN"
    assert documents[-1].warning == "\n".join([
        f"THE LABELLER WROTE SPEC APPROVAL {signed.spec_approval_id}{independent}",
        "THE KEY IS TOKENISED AND NO PATH WAS SHOWN IN CLEAR: LABELLERS COULD NOT FIND RECORDS IN THE TRUTH"
        " SOURCE",
    ])  # fmt: skip
    assert "tok:identifier:" in row["sheet"] and b"tok:" not in files["accuracy/labels.csv"]


async def test_a_labelling_that_no_longer_hashes_gets_no_pack(pack_ctx: Context) -> None:
    signed, row = await labelled_cycle(pack_ctx)
    async with pack_ctx.pool.connection() as conn:  # as only an operator who disables the trigger can
        await conn.execute("ALTER TABLE ds.labellings DISABLE TRIGGER labellings_lifecycle")
        await conn.execute(
            "UPDATE ds.labellings SET sheet = replace(sheet, 'B_RIGHT', 'A_RIGHT') WHERE labelling_id = %s",
            (row["labelling_id"],),
        )
        await conn.execute("ALTER TABLE ds.labellings ENABLE TRIGGER labellings_lifecycle")
        run = await repo.get_run(conn, signed.run_id)
        problems = await pack.problems(pack_ctx, conn, run, 1)  # type: ignore[arg-type]
    assert problems == [
        f"the attested labelling {row['labelling_id']} does not hash to its recorded labels_sha256 and"
        " score_sha256: no pack"
    ]
    with pytest.raises(pack.PackRefused, match="does not hash to its recorded labels_sha256"):
        await pack.issue(pack_ctx, signed.run_id, 1, who=signed.people["olga"])


async def test_a_sign_off_requested_before_013_packs_as_before(
    pack_ctx: Context, documents: list[Document]
) -> None:
    """Attests format 2: no accuracy file, manifest member or section, and no verify line."""
    signed = await signed_cycle(pack_ctx, sign=False)

    async def format_2(conn: repo.Conn, run: repo.Row, cycle: int, spec: Any) -> dict[str, Any]:
        return await approvals._format_2(conn, run, cycle, spec, None)

    people = signed.people
    signoff_id = await sign_off(
        pack_ctx, signed.run_id, 1, signed.spec, people["olga"], people["erin"], attests=format_2
    )
    issued = await pack.issue(pack_ctx, signed.run_id, 1, who=people["olga"])
    _, data = await pack.stored(pack_ctx, issued["pack_id"])
    files = files_of(data)
    async with pack_ctx.pool.connection() as conn:
        signoff = await repo.get_approval(conn, signoff_id)
    assert signoff is not None and signoff["attests"]["format"] == 2
    assert not [name for name in files if name.startswith("accuracy/")]
    assert "accuracy" not in json.loads(files["manifest.json"])
    assert SECTION not in [section.title for section in documents[-1].sections]
