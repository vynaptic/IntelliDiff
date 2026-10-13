"""Cut-over evidence packs: one signed ZIP per signed-off census or sampled cycle, which
an auditor checks offline, years later, with stock tools and the site's key register.

``problems`` gives every reason a cycle gets no pack; ``issue`` builds the pack in memory
and stores it in one audited transaction that reads the cycle, then the sign-off, FOR SHARE. A pack
holds facts bound to audited hashes, masked aggregates, the audit rows of its objects, the licence it was
issued under and a certificate; its SHA256SUMS is signed by the installation's
key (``security.signing``), whose fingerprint the register lists. A sampled cycle's pack also states its
frame, its draw (whose salt and selection the certificate's hand check compares with its ``sample.draw``
audit row), each sign-off stratum's bounds and the population's other draws, and claims only what a sample
shows. A sign-off of attests format 3 adds the labelling it attests, observed:
its labels without keys, its score and what they claim. Per key, a pack carries
only masked keys: the attested reviewer sheet's (at most 500, which its score's disagreements
repeat), the key of each ``key.view`` audit row of the run (a row each time a viewer opened a key, in any
cycle; the segment is verbatim, bound by the chain) and each AI answer's of the cycle, never a sampled key,
a key's labels or a frame key; its CSVs leave out root causes' and graph rows' key tokens
(``write_csv(keyed=False)``). ``raw_values`` refuses a raw key-shaped value in any text file.
``revocations`` signs the list of revoked packs, and ``pack_verify`` checks a pack offline as
``VERIFY.txt`` does.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import zipfile
import zlib
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from deltasleuth import approvals, licence
from deltasleuth.auth.principal import Principal
from deltasleuth.connectors.base import utc_iso
from deltasleuth.connectors.tables import manifest, relation
from deltasleuth.context import Context
from deltasleuth.core import jsonio, sampling
from deltasleuth.core.canonical.encoding import sha256_hex
from deltasleuth.core.sampling import percent
from deltasleuth.core.spec import listing_side
from deltasleuth.core.types import CompiledSpec, Side
from deltasleuth.reports import accuracy
from deltasleuth.reports.adjudicate import EXIT_MINIMUM
from deltasleuth.reports.csv import write_csv
from deltasleuth.reports.data import counters_section
from deltasleuth.reports.pdf import AI_LABEL, _counters_grid
from deltasleuth.reports.render import Document, Section, render_pdf
from deltasleuth.runner import census, sample
from deltasleuth.security import rawvalues
from deltasleuth.security.policy import BUSINESS_CODE
from deltasleuth.security.signing import PackSigner
from deltasleuth.store import chain, repo

FORMAT = "deltasleuth-pack/1"
PROCEDURE_NAME = f"{FORMAT} verification"
PROCEDURE = (Path(__file__).with_name("templates") / "pack-procedure.txt").read_text(encoding="utf-8")
PACKS = 9  # 009_packs: only runs created after it get a pack
MIB = 1 << 20
MAX_BYTES = 100 * MIB  # a pack's files, uncompressed
UNLISTED = ("SHA256SUMS", "SHA256SUMS.sig", "signing-key.pem")
CSV_KINDS = ("root_causes", "coverage", "graph")
NOT_CONFIGURED = "evidence packs are not configured: set packs.signing_key_ref"
PROVES = (
    "Every statement records what the DeltaSleuth installation {name} observed and stored. The signature"
    " proves that the holder of the key with fingerprint {fp} issued these files unchanged. The audit rows"
    " are unaltered and complete only as checked against your SIEM (step 7) or, on the host, an earlier"
    " recorded head. Nothing here proves the comparison right beyond the recorded facts (adjudication"
    " sampled); agreement outside the compared population, filters, columns and time window, or beyond side"
    " a's census as of its times; true times (the installation's clock); that the key was not stolen or"
    " copied, or history not rewritten before issue by the database owner; that the pack is still valid"
    " (check revocation); or a certified claim, which needs a frozen cut."
)
# A sampled cycle's; {absent} is the frame keys absent on both sides, summed
# over the sign-off strata.
PROVES_SAMPLED = (
    "Every statement records what the DeltaSleuth installation {name} observed and stored. The signature"
    " proves that the holder of the key with fingerprint {fp} issued these files unchanged. The audit rows"
    " are unaltered and complete only as checked against your SIEM (step 7) or, on the host, an earlier"
    " recorded head. Parity was observed on a sample, not a census. After a spec approval fixed the strata"
    " and sizes, the installation drew a salt holding 128 random bits and committed its SHA-256 to the audit"
    " trail in the transaction that stored the sample, before any key was compared; in each sampled sign-off"
    " stratum the keys compared are the frame's keys with the smallest SHA-256 of that salt and the key, the"
    " frame being the keys its sources held when read (a table in one snapshot, a listing as returned). Each"
    " sampled stratum's one-sided 95% Clopper-Pearson upper bound on the share of its frame keys that such"
    " reads find discrepant (keys a bug rule suppresses included), computed from its cleanly compared keys,"
    " is within its target; a stratum compared whole is judged on its exact count, its unresolved keys"
    " counted as discrepant. {absent} frame keys found absent on both sides were not judged and are outside"
    " every bound but the worst case. Taking SHA-256 as a random function and leaving unresolved and absent"
    " keys aside, had any stratum's share been above its target, a sign-off would have passed with at most a"
    " 5% chance: that is a property of the procedure, not a probability that parity holds; the bounds are"
    " separate statements, not all guaranteed at once; and a pass that follows failed draws without a real"
    " fix is weaker than 95% (the earlier draws of this population under any spec, and the fixes recorded"
    " for them, are listed; that each fix was real is the approvers' judgement). Nothing here proves the"
    " comparison right beyond the recorded facts (adjudication sampled); a bound for the strata pooled;"
    " agreement for keys outside the frame (created after it was read, held only by a side it did not list,"
    " or not returned by a listing), for keys left unresolved after retries or absent on both sides (a"
    " worst-case bound counting them discrepant is shown), or outside the compared population, filters,"
    " columns and time window; true times (the installation's clock); that the key was not stolen or"
    " copied, or history not rewritten before issue by the database owner; that the pack is still valid"
    " (check revocation); or a certified claim, which needs a frozen cut."
)
# The same of a frame whose table's engine could not pin it (a MySQL view, a table outside InnoDB): no
# certificate says such a table was read in one snapshot.
PROVES_SAMPLED_UNPINNED = PROVES_SAMPLED.replace(
    "a table in one snapshot", "a table in one pass not observed to be a snapshot"
)
# The certificate alone says it of a file side: its closing hash is the scan's.
SINGLE_KEYS = (
    "reads of single keys before and after it were checked by size, times and inode, not hashed again"
)
REVOCATIONS = "# DeltaSleuth pack revocations, key {fp}, as of {at}"
# A key list no side is counted by, by its run's keys_source: where its keys came from.
_GIVEN = {
    "explicit": "were given with the run",
    "list": "are the spec's inline list",
    "table": "are the spec's key table's",
}
_NUMBER = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9]*")
# The manifest's fixed text, which the raw-value gate does not search, the licence's words too.
_FIXED = {("format",), ("procedure",), ("claim_text",), ("installation",), ("signing_key", "algorithm"),
          *(("licence", name) for name in ("customer", "order", "tier", "state")),
          *(("subject", "sides", side, "files", "sha256_of") for side in "ab")}  # fmt: skip


class PackUnavailable(Exception):
    """Evidence packs are not configured: the API answers 503."""


class PackRefused(Exception):
    """The cycle gets no pack now; the message names every reason: the API answers 409."""


class PackForbidden(Exception):
    """Only a signed-in person issues a pack: the API answers 403."""


@dataclass(frozen=True)
class Pack:
    """A pack built in memory, not signed yet: its listed files by path inside its folder."""

    pack_id: str
    run_id: str
    cycle: int
    signoff_id: str
    attests_sha256: str
    adjudication_id: str | None
    head: dict[str, Any]  # the audit chain's head its segment ends at: {seq, hash}
    issued_at: datetime
    manifest: dict[str, Any]
    files: dict[str, bytes]


@dataclass(frozen=True)
class Sealed:
    """A signed pack: its ZIP and the hashes its record and audit row name."""

    sums: bytes
    zip: bytes
    sums_sha256: str
    manifest_sha256: str
    zip_sha256: str


def sso(ctx: Context) -> None:
    """PackRefused outside oidc mode: packs certify sign-offs, which need SSO, and so do the adjudications
    a sign-off attests."""
    if (mode := ctx.settings.auth_mode) != "oidc":
        raise PackRefused(
            f"evidence packs need SSO (auth.oidc); this deployment runs in {approvals.MODES[mode]}"
        )


def available(ctx: Context) -> PackSigner:
    """The signing key; PackRefused outside oidc mode (first), PackUnavailable without the pack settings."""
    sso(ctx)
    if ctx.signer is None:
        raise PackUnavailable(NOT_CONFIGURED)
    return ctx.signer


# -- the refusals -------------------------------------------------------------------------


async def problems(ctx: Context, conn: repo.Conn, run: repo.Row, cycle: int) -> list[str]:
    """Every reason the run's ended ``cycle`` gets no pack now (the licence's first), empty when it gets
    one; the pack is built to judge its audit trail, its raw values and its size. CyclePurged once the
    cycle's purge has started; PackRefused or PackUnavailable as ``available`` says."""
    return (await _judged(ctx, conn, run, cycle, None))[0]


async def _judged(
    ctx: Context, conn: repo.Conn, run: repo.Row, cycle: int, who: Principal | None
) -> tuple[list[str], Pack | None]:
    """``problems``, and the pack (issued by ``who``) when the run was created after 009 and the cycle has
    a fresh sign-off of format 1, 2 or 3 to build it from."""
    from deltasleuth.runner.purge import refuse_purged  # the runner's modules import this one

    signer = available(ctx)
    run_id = run["run_id"]
    at = f"cycle {cycle} of run {run_id}"
    await refuse_purged(conn, run_id, cycle)
    current = await licence.standing(ctx, conn)  # the licence at issue
    found = licence.pack_problems(current, run)
    early = run["created_at"] < await repo.applied_at(conn, PACKS)  # type: ignore[operator]
    if early:  # its scan state may lack what a census needs: the pack is not built
        found.append(f"run {run_id} started before evidence packs (009): only later runs get a pack")
    signoff = await repo.signed_off(conn, run_id, cycle)
    if signoff is None:
        return [*found, f"{at} has no approved sign-off"], None
    spec = await ctx.spec(conn, run["spec_id"])
    recorded, signoff_id = signoff["attests"], signoff["approval_id"]
    number = recorded.get("format", 0)
    rebuilt = await approvals.rebuilt(conn, run, cycle, spec, recorded)
    fresh = rebuilt is not None and approvals.attests_sha256(rebuilt) == signoff["attests_sha256"]
    signed = f"the sign-off {signoff_id} of run {run_id} cycle {cycle}"
    again = "revoke it and request the sign-off again"
    if number == 0:
        found.append(f"{signed} was signed before evidence packs (attests format 0): {again}")
    elif not fresh:
        found.append(
            f"{signed} is stale (rules changed since it was signed under attests format {number}): {again}"
        )
    found += await approvals.signoff_problems(
        conn, run, cycle, spec, cited=recorded["spec_approval_id"], later=False
    )
    if number not in (1, 2, 3) or not fresh or early:
        return found, None
    assert rebuilt is not None
    scan, ended = await repo.get_scan(conn, run_id, cycle), await repo.get_cycle(conn, run_id, cycle)
    last = await repo.last_census(conn, run_id, cycle)
    if why := census_problem(run, cycle, spec, rebuilt["census"], scan, ended, last):
        found.append(why)
    results = await repo.count_results(conn, run_id, cycle)
    attested = rebuilt["adjudication"]
    adjudication = attested and await repo.cycle_adjudication(
        conn, run_id, cycle, attested["adjudication_id"]
    )
    if results >= EXIT_MINIMUM and attested is None:
        found.append(
            f"{at} has no scored adjudication in its sign-off {signoff_id}: revoke it, draw and score a"
            " reviewer sheet, then request the sign-off again"
        )
    elif results >= EXIT_MINIMUM and not attested["passed"]:
        reasons = "; ".join(adjudication["score"]["exit"]["reasons"]) if adjudication else "not found"
        found.append(
            f"the attested adjudication {attested['adjudication_id']} did not pass ({reasons}): no pack"
        )
    named = rebuilt.get("accuracy")  # attests format 3
    labelling = named and await repo.cycle_labelling(conn, run_id, cycle, named["labelling_id"])
    facts = await _facts(
        ctx, conn, run, cycle, spec, signoff, rebuilt, adjudication, labelling, results, who, current
    )
    found += facts.problems
    pack = _build(facts, signer, ctx.settings.packs.installation)  # type: ignore[union-attr]
    if (blind := rawvalues.no_pack(run["spec_id"], spec)) is not None:
        found.append(blind)
    elif where := raw_values(spec, pack.files):
        found.append(
            f"the pack's texts hold {len(where)} raw key-shaped value(s) ({where[0]}): no pack carries them"
        )
    size = sum(map(len, pack.files.values()))
    if size > MAX_BYTES:
        found.append(f"the pack would be {math.ceil(size / MIB)} MiB, over the {MAX_BYTES // MIB} MiB limit")
    return found, pack


def census_problem(
    run: Mapping[str, Any],
    cycle: int,
    spec: CompiledSpec,
    counted: Mapping[str, Any],
    scan: Mapping[str, Any] | None,
    ended: Mapping[str, Any] | None,
    last: Mapping[str, Any] | None,
) -> str | None:
    """Why the attested census (``counted``) does not count side a in this cycle; None when it does
    (or when the sign-off's own problems already say why a scan counted nothing). ``last`` is the newest
    earlier cycle with a census (``repo.last_census``)."""
    at = f"cycle {cycle} of run {run['run_id']}"
    a, b = counted.get("a"), counted.get("b")
    if counted.get("cycle") != cycle:  # records carry their cycle: another cycle's refuses
        return _not_own(at, counted.get("at"), counted.get("cycle"))
    if a is None and b is None and last is not None:  # a listing a non-rolling soak does not walk again
        return _not_own(at, last["census"].get("at"), last["cycle"])
    why = _uncounted(run, spec, a, b, scan, ended)
    return None if why is None else f"{at} is not a census: {why}"


def _uncounted(
    run: Mapping[str, Any],
    spec: CompiledSpec,
    a: Mapping[str, Any] | None,
    b: Mapping[str, Any] | None,
    scan: Mapping[str, Any] | None,
    ended: Mapping[str, Any] | None,
) -> str | None:
    params = run["params"]
    source, tables = params.get("keys_source"), params.get("tables") or {}
    if a is not None and "failed" in a:
        return f"side a's key census failed ({a['failed']})"
    if a is not None:
        if a.get("rejected"):  # a key on such a line that side b lacks is in no result
            return f"side a's files hold {a['rejected']} line(s) that were not rows"
        if a["basis"] == "table" and a["missing"]:
            return (
                f"its key set lacks {a['missing']} key(s) of side a's table {a['relation']} (counted"
                f" {_moment(a['at'])})"
            )
        return None
    if source == "table_scan":
        complete = scan is not None and scan["done"] and (ended or {}).get("keys_cut") is None
        return "graph parity (L2) did not run" if complete and spec.raw.get("graph") else None
    if source == "hash_sample":  # its draw recorded a census of the sides its frame enumerates
        return (
            "its sampling frame does not enumerate side a (the baseline); a frame of side b cannot show keys"
            " side b lost"
        )
    if tables.get("a"):
        return "side a's key census is pending"
    if source == "paginated_endpoint":
        target = listing_side(spec.raw["key"]["source"]).target
        if target != spec.sides[Side.A].target:
            return f"its key listing reads target {target}, not side a's"
    if not tables and source in _GIVEN:
        return f"its keys {_GIVEN[source]} and no side is a table to check them against"
    return "side a (the baseline) was not counted; a count of side b cannot show keys side b lost"


def _not_own(at: str, counted: str | None, cycle: Any) -> str:
    day = f"{datetime.fromisoformat(counted):%Y-%m-%d}" if counted else "never"
    return f"{at} has no census of its own (last counted {day}, at cycle {cycle})"


# -- the facts a pack states -----------------------------------------------------------------------------


@dataclass(frozen=True)
class _Facts:
    """What the builder writes, gathered from the store in one pass."""

    pack_id: str
    issued_at: datetime
    who: Principal | None
    run: repo.Row
    cycle: int
    spec: CompiledSpec
    stored_spec: repo.Row
    signoff: repo.Row
    attests: dict[str, Any]
    spec_approval: repo.Row
    adjudication: repo.Row | None
    adjudications: list[repo.Row]  # every draw of the cycle, oldest first
    labelling: repo.Row | None  # the attested one
    labellings: list[repo.Row]  # every labelling of the cycle, oldest first
    results: int
    retention: dict[str, Any]
    earlier: list[repo.Row]  # the cycle's packs issued before
    head: dict[str, Any]
    upgrade: int
    objects: list[str]
    segment: list[repo.Row]
    grants: list[dict[str, Any]]  # the approval.grant rows of the decisions, parsed
    issuers: dict[str, str]
    answers: list[dict[str, Any]]
    csvs: dict[str, bytes]
    problems: list[str]
    licence: dict[str, Any] | None  # the manifest's licence member: the one at issue
    later: list[repo.Row]  # a sampled cycle's later draws at issue, as the population's ledger lists them


async def _facts(
    ctx: Context,
    conn: repo.Conn,
    run: repo.Row,
    cycle: int,
    spec: CompiledSpec,
    signoff: repo.Row,
    attests: dict[str, Any],
    adjudication: repo.Row | None,
    labelling: repo.Row | None,
    results: int,
    who: Principal | None,
    current: licence.Standing,
) -> _Facts:
    from deltasleuth.runner.purge import days

    run_id, found = run["run_id"], []
    stored_spec = await repo.get_spec(conn, run["spec_id"])
    spec_approval = await repo.get_approval(conn, attests["spec_approval_id"])
    assert stored_spec is not None and spec_approval is not None
    if _spec_json(stored_spec["body"]) != stored_spec["content_hash"]:
        found.append(
            f"spec {run['spec_id']}'s stored body does not re-hash to its content hash (edited, or not"
            " storable as uploaded): upload a changed version (a new version)"
        )
    if adjudication is not None and (
        hashlib.sha256(adjudication["sheet"].encode()).hexdigest() != adjudication["sheet_sha256"]
        or sha256_hex(jsonio.dumps(adjudication["score"], sort_keys=True)) != adjudication["score_sha256"]
    ):
        found.append(
            f"the attested adjudication {adjudication['adjudication_id']} does not hash to its recorded"
            " sheet_sha256 and score_sha256: no pack"
        )
    adjudications = sorted(
        await repo.list_adjudications(conn, run_id, cycle), key=lambda j: j["adjudication_id"]
    )
    labellings = sorted(
        await repo.list_labellings(conn, run_id, cycle, full=True), key=lambda v: v["labelling_id"]
    )
    earlier = await repo.cycle_packs(conn, run_id, cycle)
    signoffs = await repo.approval_history(conn, run_id=run_id, cycle=cycle)  # revoked ones too
    kept = days(ctx)
    ended = next(c for c in await repo.list_cycles(conn, run_id, **kept) if c["cycle"] == cycle)
    head = await repo.audit_head(conn)
    assert head is not None  # the run's creation wrote rows
    decided = {signoff["approval_id"], spec_approval["approval_id"]}
    drawn = attests.get("sample")  # a sampled cycle's draw
    objects = sorted(
        {run_id, spec_approval["approval_id"], *(s["approval_id"] for s in signoffs)}
        | {str(spec_id) for spec_id in await repo.spec_lineage(conn, run["spec_id"])}
        | {j["adjudication_id"] for j in adjudications}
        | {v["labelling_id"] for v in labellings}
        | {p["pack_id"] for p in earlier}
        | {d["draw_id"] for d in (drawn or {}).get("ledger", [])}  # their sample.close rows
    )
    segment = await repo.audit_segment(conn, objects, head["seq"])
    grants = [
        {"seq": row["seq"]} | json.loads(row["text"])
        for row in segment
        if row["object"] in decided and row["action"] == "approval.grant"
    ]
    if sessions := {g["session_id"] for g in grants if g["session_id"]} - set(objects):
        objects = sorted({*objects, *sessions})
        segment = await repo.audit_segment(conn, objects, head["seq"])
    try:
        chain.check_segment(segment)
    except chain.ChainBroken as exc:
        found.append(f"the audit trail of run {run_id} does not verify at row {exc.seq}: run ds audit verify")
    if drawn is not None and not _committed(drawn, run_id, segment):
        found.append(
            f"the sample {drawn['draw_id']} of cycle {cycle} of run {run_id} does not match its sample.draw"
            " audit row: no pack"
        )
    if labelling is not None and _label_row(labelling, segment) is None:
        found.append(
            f"the attested labelling {labelling['labelling_id']} does not hash to its recorded labels_sha256"
            " and score_sha256: no pack"
        )
    answers = [a for a in await repo.chat_answers(conn, run_id) if a["cycle"] == cycle]
    if answers:  # opened only to hash them
        sealer = await ctx.sealer(conn, run_id)
        answers = [sealer.open_row(a) for a in answers]
    days_kept = kept[f"{run['retention']}_days"]
    return _Facts(
        pack_id=repo.new_pack_id(),
        issued_at=datetime.now(UTC),
        who=who,
        run=run,
        cycle=cycle,
        spec=spec,
        stored_spec=stored_spec,
        signoff=signoff,
        attests=attests,
        spec_approval=spec_approval,
        adjudication=adjudication,
        adjudications=adjudications,
        labelling=labelling,
        labellings=labellings,
        results=results,
        retention={
            "class": run["retention"],
            "days": days_kept,
            "per_key_detail_until": f"{ended['expires_at']:%Y-%m-%d}",
            "held": run["hold_reason"] is not None,
            "stored_pack": "deleted when the cycle is purged",
        },
        earlier=earlier,
        head={"seq": head["seq"], "hash": head["hash"]},
        upgrade=await repo.audit_upgrade_seq(conn),
        objects=objects,
        segment=segment,
        grants=grants,
        issuers=await repo.issuers(conn, [g["actor"] for g in grants]),
        answers=[
            {
                "created_at": utc_iso(a["created_at"]),
                "cycle": a["cycle"],
                "key_masked": a["key_masked"],
                "citations": a["citations"],
                "content_sha256": sha256_hex(a["content"]),
            }
            for a in answers
        ],
        csvs={kind: await write_csv(ctx, run_id, cycle, kind, keyed=False) for kind in CSV_KINDS},
        problems=found,
        licence=_licence(current),
        later=[] if drawn is None else await sample.after(conn, drawn["population"], drawn["draw_id"]),
    )


def _committed(drawn: Mapping[str, Any], run_id: str, segment: Sequence[repo.Row]) -> bool:
    """Whether the draw's ``sample.draw`` row in the segment (chain row ``draw_seq``, the run's) names it and
    holds the SHA-256 of its salt, ``label:nonce``, and of its selection."""
    row = next((json.loads(r["text"]) for r in segment if r["seq"] == drawn["draw_seq"]), None)
    detail = (row and (row["action"], row["object"]) == ("sample.draw", run_id) and row["detail"]) or {}
    salt = sha256_hex(sampling.salt(drawn["salt_label"], drawn["nonce"]))
    committed = (detail.get("draw_id"), detail.get("salt_sha256"), detail.get("selection_sha256"))
    return committed == (drawn["draw_id"], salt, drawn["selection_sha256"])


def _label_row(labelling: Mapping[str, Any], segment: Sequence[repo.Row]) -> int | None:
    """The chain row of the labelling's ``accuracy.label`` in the segment; None unless its kept sheet's
    labels and its score hash to the SHA-256 values it records and that row holds."""
    recorded = {name: labelling[name] for name in ("labels_sha256", "score_sha256")}
    hashed = {
        "labels_sha256": sha256_hex(accuracy.keyless(labelling["sheet"])),
        "score_sha256": sha256_hex(jsonio.dumps(labelling["accuracy"], sort_keys=True)),
    }
    for row in segment:
        if (row["object"], row["action"]) == (labelling["labelling_id"], "accuracy.label"):
            detail = json.loads(row["text"])["detail"]
            held = {name: detail.get(name) for name in recorded}
            return row["seq"] if held == recorded == hashed else None
    return None


def _licence(current: licence.Standing) -> dict[str, Any] | None:
    """The licence named and its state (``pairs`` None when unlimited); None in evaluation, which gets no
    pack, and in an open build, whose packs name no licence."""
    if (found := current.licence) is None:
        return None
    fields = found.fields()
    names = ("licence_id", "customer", "order", "installation_id", "tier", "pairs", "starts", "ends", "key")
    return {name: fields[name] for name in names} | {"state": current.state}


def _spec_json(body: Any) -> str:
    """The sha256 of ``facts/spec.json``: ``assemble.content_hash``'s form of the body."""
    return sha256_hex(_spec_text(body))


def _spec_text(body: Any) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)


def verify_text(
    pack_id: str, installation: str, signoff_id: str, spec_approval_id: str, head: int, upgrade: int
) -> str:
    """``VERIFY.txt``: two lines for this pack, then the canonical procedure ``ds packs key`` writes."""
    return (
        f"DeltaSleuth evidence pack {pack_id} of installation {installation}. Unzip it, cd {pack_id}/, then"
        " run in sh the copy of the procedure below filed with your key register; its step 0 reads line 2:\n"
        f"PACK={pack_id} SIGNOFF={signoff_id} SPECAPP={spec_approval_id} HEAD={head} UPGRADE={upgrade}\n"
        + PROCEDURE
    )


def _build(facts: _Facts, signer: PackSigner, installation: str) -> Pack:
    """The pack's listed files: facts, adjudication, audit segment, masked CSVs, the AI annex,
    VERIFY.txt, the certificate and, last, the manifest naming the others' hashes; under attests format 3,
    the cycle's labellings and the attested one's keyless labels and score."""
    run, attests = facts.run, facts.attests
    files: dict[str, bytes] = {
        "facts/attests.json": jsonio.dumps(attests, sort_keys=True).encode(),
        "facts/spec.json": _spec_text(facts.stored_spec["body"]).encode(),
        "facts/plan.json": approvals.plan(run["params"]).encode(),
        "adjudication/attempts.json": _json(_attempts(facts.adjudications)),
        "audit/chain.tsv": "".join(
            f"{r['seq']}\t{r['prev']}\t{r['obj_prev']}\t{r['hash']}\t{r['object']}\t{r['text']}\n"
            for r in facts.segment
        ).encode(),
        "annex/ai_answers.json": _json({"label": AI_LABEL, "answers": facts.answers}),
        **{f"csv/{kind}.csv": data for kind, data in facts.csvs.items()},
    }
    if (adjudication := facts.adjudication) is not None:
        files["adjudication/sheet.csv"] = adjudication["sheet"].encode()
        files["adjudication/score.json"] = jsonio.dumps(adjudication["score"], sort_keys=True).encode()
    if "accuracy" in attests:
        files["accuracy/attempts.json"] = _json(_labellings(facts.labellings))
    if (labelling := facts.labelling) is not None:
        files["accuracy/labels.csv"] = accuracy.keyless(labelling["sheet"]).encode()
        files["accuracy/score.json"] = jsonio.dumps(labelling["accuracy"], sort_keys=True).encode()
    files["VERIFY.txt"] = verify_text(
        facts.pack_id, installation, facts.signoff["approval_id"], facts.spec_approval["approval_id"],
        facts.head["seq"], facts.upgrade,
    ).encode()  # fmt: skip
    manifest = _manifest(facts, signer, installation)
    files["certificate.pdf"] = render_pdf(_certificate(facts, manifest, files["VERIFY.txt"].decode()))
    manifest["files"] = {path: hashlib.sha256(data).hexdigest() for path, data in sorted(files.items())}
    files["manifest.json"] = (jsonio.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    return Pack(
        pack_id=facts.pack_id,
        run_id=run["run_id"],
        cycle=facts.cycle,
        signoff_id=facts.signoff["approval_id"],
        attests_sha256=facts.signoff["attests_sha256"],
        adjudication_id=adjudication and adjudication["adjudication_id"],
        head=facts.head,
        issued_at=facts.issued_at,
        manifest=manifest,
        files=files,
    )


def _json(value: Any) -> bytes:
    return (jsonio.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _attempts(adjudications: Sequence[repo.Row]) -> list[dict[str, Any]]:
    """Every draw of the cycle, scored or not, without its keys (``adjudication/attempts.json``)."""
    names = ("adjudication_id", "drawn_by", "drawn_by_name", "sample_limit", "salt", "scored_by",
             "scored_by_name", "upload_sha256", "sheet_sha256", "score_sha256", "passed")  # fmt: skip
    return [
        {name: j[name] for name in names}
        | {
            "drawn_at": utc_iso(j["drawn_at"]),
            "scored_at": j["scored_at"] and utc_iso(j["scored_at"]),
            "candidates": j["selection"]["candidates"],
            "unrepresented": j["selection"]["unrepresented"],
            "adjudicated": j["score"] and j["score"]["adjudicated"],
            "agreed": j["score"] and j["score"]["agreed"],
        }
        for j in adjudications
    ]


def _labellings(labellings: Sequence[repo.Row]) -> list[dict[str, Any]]:
    """Every labelling of the cycle, labelled or not, without its keys (``accuracy/attempts.json``)."""
    names = ("labelling_id", "drawn_by", "drawn_by_name", "size", "per_cause", "accepted", "agreed", "salt",
             "truth_source", "selection_sha256", "labelled_by", "labelled_by_name", "sheet_sha256",
             "labels_sha256", "score_sha256")  # fmt: skip
    return [
        {name: v[name] for name in names}
        | {
            "truth_as_of": f"{v['truth_as_of']}",
            "names_side": v["selection"]["names_side"],
            "unlocatable": v["selection"]["unlocatable"],
            "drawn_at": utc_iso(v["drawn_at"]),
            "labelled_at": v["labelled_at"] and utc_iso(v["labelled_at"]),
            "counts": v["selection"]["counts"],
            "labels": v["accuracy"]
            and {
                name: found and {label: s["x"] for label, s in found["labels"].items()}
                for name in accuracy.POPULATIONS
                for found in [v["accuracy"][name]]
            },
        }
        for v in labellings
    ]


def _subject(facts: _Facts) -> dict[str, Any]:
    """What the pack is about; a table side on MySQL or Oracle, and the other side of its pair, also
    names its engine and its server's major and minor version: the one the cycle's scan, census or frame
    read, else the plan's. A file side adds its ``files`` from the plan: their
    count, their bytes, the one digest of them all, what that digest is the SHA-256 of, and the time their
    stamp file declares."""
    tables, counted = facts.run["params"].get("tables") or {}, facts.attests.get("census") or {}
    return {
        "run_id": facts.run["run_id"],
        "cycle": facts.cycle,
        "basis": "census" if facts.attests.get("sample") is None else "sample",
        "spec": {
            "spec_id": facts.run["spec_id"],
            "name": facts.spec.name,
            "version": facts.spec.version,
            "content_hash": facts.spec.content_hash,
        },
        "sides": {
            side.value: {
                "role": cfg.role,
                "target": cfg.target,
                "relation": relation(tables[side.value]) if tables.get(side.value) else None,
            }
            | {name: plan[name] for name in ("engine", "server") if name in plan}
            | ({"server": read["server"]} if "engine" in plan and read.get("server") else {})
            | ({"files": _files(plan)} if plan.get("engine") == "files" else {})
            for side, cfg in facts.spec.sides.items()
            for plan, read in [(tables.get(side.value) or {}, counted.get(side.value) or {})]
        },
    }


def _files(plan: Mapping[str, Any]) -> dict[str, Any]:
    """``sha256_of``: how the digest is formed and the command that prints it, where it is shown: a holder
    of the one file of a side would else compare it with that file's own SHA-256."""
    return {
        "count": plan["file_count"],
        "bytes": plan["bytes"],
        "sha256": plan["files_sha256"],
        "sha256_of": manifest.SHA256_OF_PATHS if plan["file"].get("partitions") else manifest.SHA256_OF,
        "as_of": plan["as_of"],
    }


def _decisions(facts: _Facts) -> list[dict[str, Any]]:
    """The spec approval and the sign-off: who asked, and each step granted, with the grant's session."""
    found = []
    for approval in (facts.spec_approval, facts.signoff):
        steps = [
            {
                "step": g["detail"]["step"],
                "of": g["detail"]["of"],
                "actor": g["actor"],
                "name": g["actor_name"],
                "issuer": facts.issuers.get(g["actor"]),
                "session_id": g["session_id"],
                "auth_time": g["detail"].get("auth_time"),
                "at": g["at"],
                "audit_seq": g["seq"],
            }
            for g in facts.grants
            if g["object"] == approval["approval_id"]
        ]
        found.append({
            "approval_id": approval["approval_id"],
            "kind": approval["kind"],
            "requested_by": approval["requested_by"],
            "requested_at": utc_iso(approval["requested_at"]),
            "steps": steps,
        })  # fmt: skip
    return found


def _manifest(facts: _Facts, signer: PackSigner, installation: str) -> dict[str, Any]:
    """``manifest.json`` but its ``files``; a sampled cycle's adds its ``sample`` (as attested) and
    ``later_draws``, the population's draws after it at issue; under attests
    format 3, ``accuracy``: whether a labelling is attested and the cycle's labellings, counted."""
    attests, attested, who = facts.attests, facts.attests["adjudication"], facts.who
    scan, drawn = attests["scan"] or {}, attests.get("sample")
    claim = _claim(attests, signer, installation)
    sampled = drawn and {
        "sample": drawn,
        "later_draws": [
            {name: d[name] for name in ("draw_id", "spec_name", "run_id", "cycle", "status")}
            | {"drawn_at": utc_iso(d["drawn_at"])}
            for d in facts.later
        ],
    }
    labelled = "accuracy" in attests and {
        "accuracy": {
            "attested": facts.labelling is not None,
            "draws": len(facts.labellings),
            "labelled": sum(v["labelled_at"] is not None for v in facts.labellings),
        }
    }
    return (
        {
            "format": FORMAT,
            "procedure": PROCEDURE_NAME,
            "pack_id": facts.pack_id,
            "installation": installation,
            "claim": "observed",
            "claim_text": claim,
            "issued_at": utc_iso(facts.issued_at),
            "issued_at_epoch": int(facts.issued_at.timestamp()),
            "issued_by": {"actor": who and who.actor, "name": who and who.name},
            "signing_key": {
                "fingerprint": signer.fingerprint,
                "algorithm": "ECDSA P-256, SHA-256 (RFC 6979)",
            },
            "subject": _subject(facts),
            "census": attests["census"],
            "results": {
                "per_key_results": facts.results,
                "matched_by_row_hash": scan.get("matched"),
                "counters": attests["counters"],
                "unexplained_critical": attests["unexplained_critical"],
            },
            "decisions": _decisions(facts),
            "adjudication": {
                "attested": attested,
                "draws": len(facts.adjudications),
                "scored": sum(j["scored_at"] is not None for j in facts.adjudications),
                "sampled": attested and attested["adjudicated"],
            },
            "audit": {"head": facts.head, "upgrade_seq": facts.upgrade, "objects": facts.objects},
            "retention": facts.retention,
            "licence": facts.licence,
            "previous_packs": [
                {name: p[name] for name in ("pack_id", "sums_sha256", "status")}
                | {"issued_at": utc_iso(p["issued_at"])}
                for p in facts.earlier
            ],
        }
        | (sampled or {})
        | (labelled or {})
    )


def _claim(attests: Mapping[str, Any], signer: PackSigner, installation: str) -> str:
    """``claim_text``: a sampled cycle's says ``in one snapshot`` only of a frame whose every record was
    read in one (a record that names no ``consistency`` was: PostgreSQL's)."""
    drawn = attests.get("sample")
    if drawn is None:
        return PROVES.format(name=installation, fp=signer.fingerprint)
    frame = [found for found in (attests["census"] or {}).values() if isinstance(found, dict)]
    pinned = all(found.get("consistency", "snapshot") == "snapshot" for found in frame)
    proves = PROVES_SAMPLED if pinned else PROVES_SAMPLED_UNPINNED
    return proves.format(name=installation, fp=signer.fingerprint, absent=f"{_absent(drawn):,}")


def _absent(drawn: Mapping[str, Any]) -> int:
    """The frame keys found absent on both sides, summed over the draw's sign-off strata."""
    return sum(s["absent_both"] for s in (drawn["judgement"] or {}).get("strata", []))


# -- the certificate --------------------------------------------------------------------------


def _moment(at: str | datetime | None) -> str:
    if at is None:
        return "-"
    moment = datetime.fromisoformat(at) if isinstance(at, str) else at
    return f"{moment.astimezone(UTC):%Y-%m-%d %H:%M} UTC"


def _certificate(facts: _Facts, manifest: Mapping[str, Any], verify: str) -> Document:
    """One ``render.Document``: every section is observed, and the signature's reach is stated verbatim. A
    sampled cycle's states its frame instead of a census, its draw and bounds, and the population's draws."""
    run, attests, spec, drawn = facts.run, facts.attests, facts.spec, facts.attests.get("sample")
    subject = manifest["subject"]
    lines, warnings = _census_lines(spec, attests["census"], run["params"].get("tables") or {})
    counted = [Section("Census (observed)", lines)]
    if drawn is not None:
        frame = drawn["frame"]
        lines.append(
            f"Frame: {frame['keys']:,} distinct keys (a only {frame['origin']['a']:,}; b only"
            f" {frame['origin']['b']:,}; both {frame['origin']['both']:,}); {frame['unfit']:,} outside"
            " key.pattern"
        )
        counted = [
            Section("Frame (observed)", lines),
            _sample_section(facts, drawn),
            Section("Draws of this population (observed)", _draw_lines(facts, drawn)),
        ]
        warnings += _sample_warnings(facts, drawn)
    labelled = []
    if "accuracy" in attests:  # attests format 3
        labelled = [Section("Labelled accuracy (observed)", _accuracy_lines(facts))]
        warnings += _accuracy_warnings(facts)
    critical = attests["unexplained_critical"]
    results = replace(
        _counters_grid(counters_section(attests["counters"])),
        title="Results (observed)",
        lines=[f"{facts.results:,} per-key result(s); unexplained CRITICAL keys: {critical}"],
    )
    who = manifest["issued_by"]["name"] or "-"
    meta = [
        "Claim level: observed" + (" (sampled)" if drawn is not None else ""),
        f"Installation {manifest['installation']}; issued {_moment(facts.issued_at)} by {who}",
        f"Run {run['run_id']}, cycle {facts.cycle}; spec {spec.name} {spec.version}",
    ]
    claim = [
        f"Installation: {manifest['installation']}",
        f"Signing key fingerprint (the SHA-256 of its public key, step 1):"
        f" {manifest['signing_key']['fingerprint']}: compare with your key register",
        f"Verification procedure: {PROCEDURE_NAME}",
        "Every statement is observed: live reads of each side, not a certified census.",
    ]
    if (named := manifest["licence"]) is not None:
        claim.append(
            f"Licence {named['licence_id']} of {named['customer']} (order {named['order']}): {named['tier']},"
            f" {named['starts']} to {named['ends']}, installation {named['installation_id']}"
            + ("; issued in its grace period" if named["state"] == "grace" else "")
        )
    sides = [
        f"Side {s} ({v['role']}): target {v['target']}"
        + (f", {v['relation']}" if v["relation"] else "")
        + (f" ({v['engine']} {v['server']})" if "engine" in v else "")
        for s, v in subject["sides"].items()
    ]
    warnings += _upgraded(run["params"].get("tables") or {}, subject["sides"])
    named = [
        f"Run {subject['run_id']}, cycle {facts.cycle} ({run['mode']})",
        f"Spec {spec.name} {spec.version} (spec {run['spec_id']}), content hash {spec.content_hash}",
    ]
    return Document(
        title=f"DeltaSleuth evidence pack {facts.pack_id}",
        meta="\n".join(meta),
        warning="\n".join(warnings) or None,
        sections=[
            Section("Claim (observed)", claim),
            Section("Subject (observed)", [*named, *sides]),
            *counted,
            results,
            _classes(attests),
            Section("Decisions (observed)", _decision_lines(manifest["decisions"])),
            Section("Adjudication (observed)", _adjudication_lines(facts, attests)),
            *labelled,
            Section("Audit trail (observed)", _audit_lines(facts)),
            Section("Retention (observed)", [_retention_line(facts)]),
            Section("What the signature proves (observed)", [manifest["claim_text"]]),
            Section("How to verify (observed)", verify.splitlines()),
        ],
    )


def _upgraded(tables: Mapping[str, Any], sides: Mapping[str, Any]) -> list[str]:
    """The certificate's warning of each side the cycle read on another server version than the one its
    run was planned on: the subject names the one read."""
    return [
        f"SIDE {side.upper()} READ ON {found['engine']} {found['server']}: the run was planned on"
        f" {tables[side]['server']}"
        for side, found in sides.items()
        if "engine" in found and found["server"] != tables[side]["server"]
    ]


def _census_lines(
    spec: CompiledSpec, counted: Mapping[str, Any], tables: Mapping[str, Any]
) -> tuple[list[str], list[str]]:
    """Each side's census, and the warnings a table scan read in several snapshots gets. A file side's line
    alone adds, from its plan (``tables``), the digest of its files, what it is the SHA-256 of, and how
    single keys were read."""
    lines, warnings = [], []
    for side, cfg in spec.sides.items():
        found = counted.get(side.value)
        named = f"Side {side.value} ({cfg.role}, target {cfg.target})"
        if found is None:
            lost = " (a count of side b cannot show keys side b lost)" if side is Side.B else ""
            lines.append(f"{named}: not counted{lost}")
        elif "failed" in found:
            lines.append(f"{named}: its count failed ({found['failed']})")
        else:
            fixed = ""
            if found.get("consistency") == "fixed":
                held = _files(tables[side.value])
                fixed = f"; sha256 {held['sha256']} of {held['sha256_of']}; {SINGLE_KEYS}"
            lines.append(f"{named}: {census.described(found)}{fixed}; {_counted(found)}")
        if warned := census.warning(side.value, found):
            warnings.append(warned)
    return lines, warnings


def _counted(found: Mapping[str, Any]) -> str:
    if found["basis"] == "listing":
        return f"{found['items']:,} item(s) listed"
    where = f" where {found['where']}" if found.get("where") else ""
    return f"{found['rows']:,} row(s) of {found['relation']}{where}, {found['skipped']:,} without a key"


def _day(at: str | datetime) -> str:
    moment = datetime.fromisoformat(at) if isinstance(at, str) else at
    return f"{moment.astimezone(UTC):%Y-%m-%d}"


def _sample_section(facts: _Facts, drawn: Mapping[str, Any]) -> Section:
    """What the draw committed and how to check it by hand, then each sign-off stratum as its cycle's end
    judged it, never recounted."""
    seq, approval = drawn["draw_seq"], facts.spec_approval
    lines = [
        f"Draw {drawn['draw_id']} by the installation at {_moment(drawn['drawn_at'])}, in the transaction"
        f" that stored the sample (audit row {seq}): salt {drawn['salt_label']} with nonce {drawn['nonce']},"
        f" salt SHA-256 {drawn['salt_sha256']}, selection SHA-256 {drawn['selection_sha256']};"
        f" {drawn['method']}; strata and sizes approved in {approval['approval_id']} on"
        f" {_day(approval['decided_at'])}",
        f"To check by hand: row {seq} of audit/chain.tsv is action sample.draw and holds both SHA-256 values,"
        f" and the SHA-256 of the text {drawn['salt_label']}:{drawn['nonce']} is the salt's",
    ]
    if facts.spec.key.value_class == BUSINESS_CODE:
        lines.append(
            "Keys are shown in clear here (key class business_code): with the nonce, anyone holding this pack"
            " can hash each key it lists and tell which of them lie in a stratum compared whole, a customer"
            " attribute."
        )
    judgement = drawn["judgement"] or {"strata": [], "contrast": None}
    strata = judgement["strata"]
    rows = [
        (s["label"], s["tier"], percent(s["target"]), s["basis"], s["frame_keys"], s["drawn"], s["n"], s["x"],
         s["unresolved"], s["absent_both"], s["api_uncomparable"], s["retried"],
         "compared whole" if s["ub95_one_sided"] is None else percent(s["ub95_one_sided"]), s["verdict"])
        for s in strata
    ]  # fmt: skip
    lines += [
        f"Reported, never judged: stratum {s['label']}'s bound is"
        f" {percent(s['ub95_one_sided_all_suppressed'])} counting every known or noise-only difference"
        f" discrepant, and {percent(s['ub95_one_sided_worst_case'])} counting its unresolved, absent and"
        " truncated keys discrepant (the worst case)"
        for s in strata
        if s["ub95_one_sided"] is not None
    ]
    if (contrast := judgement["contrast"]) is not None:
        lines.append(
            f"Contrast keys (compared, never bounded): {contrast['total']:,}, {contrast['compared']:,}"
            f" compared, {contrast['different']:,} discrepant."
        )
    lines.append("No bound is given for the strata pooled.")
    header = ("Stratum", "Tier", "Target", "Basis", "Frame keys", "Drawn", "Compared", "Discrepant",
              "Unresolved", "Absent both", "Truncated", "Retried", "Bound", "Verdict")  # fmt: skip
    return Section("Sample and bounds (observed)", lines, header, rows)


def _decided_later(facts: _Facts) -> tuple[int | None, list[dict[str, Any]]]:
    """The sign-off's final ``approval.grant`` row and the later draws it recorded as it judged them
    (R-c): a pack reads them, never judges them again."""
    signoff_id = facts.signoff["approval_id"]
    final = [
        g for g in facts.grants if g["object"] == signoff_id and g["detail"]["step"] == g["detail"]["of"]
    ]
    return (final[-1]["seq"], final[-1]["detail"].get("later_draws") or []) if final else (None, [])


def _draw_lines(facts: _Facts, drawn: Mapping[str, Any]) -> list[str]:
    """The population's other draws under any spec: those before this one as the sign-off attests them (R-a)
    with the fixes that closed them, those after it at the decision and at issue. The sample.draw row of a
    draw of another run is not in the segment: its number is said to be outside the pack."""
    lines, shipped = [], {row["seq"] for row in facts.segment}
    for d in drawn["ledger"]:
        outside = "" if d["draw_seq"] in shipped else ", not in this pack"
        line = f"Draw {d['draw_id']} (spec {d['spec_name']}, run {d['run_id']} cycle {d['cycle']}, audit row"
        line += f" {d['draw_seq']}{outside}), before this one: "
        if d["status"] == "passed":
            lines.append(line + "passed")
            continue
        line += sample._UNPASSED[d["status"]]
        line += f" {sample._strata(d['failed_strata'])}" if d["failed_strata"] else ""
        closed = d["closed_by"] and f"closed by {d['closed_by']} (audit row {d['close_seq']})"
        closed = closed and f"{closed}: {d['close_note']}"
        lines.append(f"{line}; {closed or 'not closed before it'}")
    if not lines:
        lines.append("No draw of this population was made before this one.")
    seq, decided = _decided_later(facts)
    listed = "; ".join(f"{d['draw_id']} {d['status']}" for d in decided) or "none"
    lines.append(f"Later draws at the decision (audit row {seq}): {listed}")
    listed = "; ".join(
        f"{d['draw_id']} (spec {d['spec_name']}, run {d['run_id']} cycle {d['cycle']}, drawn"
        f" {_moment(d['drawn_at'])}): {d['status']}"
        for d in facts.later
    )
    lines.append(f"Later draws at issue: {listed or 'none'}")
    return lines


def _sample_warnings(facts: _Facts, drawn: Mapping[str, Any]) -> list[str]:
    """The certificate's warnings of a sampled cycle: a side outside the frame, absent keys, earlier draws
    that did not pass, later draws incomplete or live at the decision, and later draws failed at issue."""
    counted = facts.attests["census"]
    found = [
        f"SIDE {s.upper()} IS NOT IN THE FRAME: keys only side {s} holds were not sampled; nothing here"
        f" bounds extra records of side {s}"
        for s in "ab"
        if counted.get(s) is None
    ]
    if absent := _absent(drawn):
        were = "S WERE" if absent > 1 else " WAS"
        found.append(
            f"{absent:,} FRAME KEY{were} ABSENT ON BOTH SIDES: NOT JUDGED, OUTSIDE EVERY BOUND BUT THE WORST"
            " CASE"
        )
    if unpassed := [d for d in drawn["ledger"] if d["status"] != "passed"]:
        others = Counter(d["spec_name"] for d in unpassed if d["spec_name"] != facts.spec.name)
        under = ", ".join(f"{n} UNDER {name}" for name, n in sorted(others.items()))
        draws = f"{len(unpassed)} EARLIER DRAW{'S' if len(unpassed) > 1 else ''}"
        found.append(f"{draws} OF THIS POPULATION DID NOT PASS{f' ({under})' if under else ''}: SEE DRAWS")
    if pending := sum(d["status"] in ("incomplete", "live") for d in _decided_later(facts)[1]):
        were = "S WERE" if pending > 1 else " WAS"
        found.append(f"{pending} LATER DRAW{were} INCOMPLETE OR LIVE AT THE DECISION: SEE DRAWS")
    found += [
        f"DRAW {d['draw_id']}, MADE AFTER THIS ONE, FAILED ON {_day(d['ended_at'])}: SEE DRAWS"
        for d in facts.later
        if d["status"] == "failed"
    ]
    return found


def _classes(attests: Mapping[str, Any]) -> Section:
    rows = [
        ("rule", r["rule_id"], r["classification"], "", f"{r['reason']} ({r['ticket']})", r["keys"])
        for r in attests["rules"]
    ]
    rows += [
        ("root cause", c["root_cause_id"], c["taxonomy_code"], c["max_severity"],
         f"{c['change_type']} {c['path_pattern']}", c["affected_keys"])
        for c in attests["root_causes"]
    ]  # fmt: skip
    lines = ["Approving the sign-off decided each rule and root cause listed here."]
    if not rows:
        return Section("Classes decided at sign-off (observed)", [*lines, "No rule and no root cause."])
    header = ("Kind", "Id", "Class", "Severity", "Detail", "Keys")
    return Section("Classes decided at sign-off (observed)", lines, header, rows)


def _decision_lines(decisions: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = []
    for d in decisions:
        what = "Spec approval" if d["kind"] == "spec" else "Sign-off"
        lines.append(
            f"{what} {d['approval_id']}: requested by {d['requested_by']} on {_moment(d['requested_at'])}"
        )
        lines += [
            f"step {s['step']} of {s['of']} granted by {s['name']} ({s['actor']}, issuer {s['issuer']}) on"
            f" {_moment(s['at'])}, session {s['session_id']}, signed in {_moment(s['auth_time'])}"
            for s in d["steps"]
        ]
    return lines


def _adjudication_lines(facts: _Facts, attests: Mapping[str, Any]) -> list[str]:
    attested, draws = attests["adjudication"], facts.adjudications
    scored = sum(j["scored_at"] is not None for j in draws)
    named = f"attested {attested['adjudication_id']}" if attested else "none attested"
    sampled = attested["adjudicated"] if attested else 0
    matched = (attests["scan"] or {}).get("matched")
    hashed = f"; {matched:,} rows matched by row hash were not sampled" if matched else ""
    lines = [
        f"draws: {len(draws)} ({scored} scored); {named}",
        f"sampled {sampled:,} of {facts.results:,} per-key results{hashed}",
    ]
    if attested:
        verdict = "passed" if attested["passed"] else "did not pass"
        lines.append(
            f"{attested['adjudicated']} adjudicated, {attested['agreed']} agreed with the engine: {verdict}"
        )
    return lines


def _accuracy_lines(facts: _Facts) -> list[str]:
    """What the attested labelling says (``accuracy.lines``) and may not be read as (``accuracy.LIMITS``),
    and how to check its files by hand; one line when none is attested: that nothing was measured, or,
    when a labelling no person labelled was, how many were drawn and labelled and where they are listed."""
    if (row := facts.labelling) is None:
        if not facts.attests["labellings"]["labelled"]:
            return ["No labelled accuracy is attested: which side is right was not measured."]
        return [
            f"No labelled accuracy is attested: {_labellings_drawn(facts).lower()}, none by a signed-in"
            " person; accuracy/attempts.json lists each with its label counts."
        ]
    seq = _label_row(row, facts.segment)
    return [
        *accuracy.lines(row, signoff=facts.signoff["approval_id"]),
        accuracy.LIMITS,
        "To check by hand: sha256sum accuracy/labels.csv accuracy/score.json prints facts/attests.json's"
        f" accuracy.labels_sha256 and accuracy.score_sha256; row {seq} of audit/chain.tsv is the"
        f" accuracy.label row of {row['labelling_id']} holding both",
    ]


def _labellings_drawn(facts: _Facts) -> str:
    """``3 LABELLINGS OF THIS CYCLE WERE DRAWN (2 LABELLED)``, as the sign-off attests them."""
    counts = facts.attests["labellings"]
    drawn = "1 LABELLING OF THIS CYCLE WAS" if counts["drawn"] == 1 else f"{counts['drawn']} LABELLINGS"
    were = "" if counts["drawn"] == 1 else " OF THIS CYCLE WERE"
    return f"{drawn}{were} DRAWN ({counts['labelled']} LABELLED)"


def _accuracy_warnings(facts: _Facts) -> list[str]:
    """The certificate's warnings of an attested labelling: other labellings of the cycle, a labeller not
    independent of the run, undecidable sampled discrepancies (5% or more, or any nobody reviewed), a
    circular truth source and records labellers could not find. With none attested: that labellings were
    labelled, none by a signed-in person."""
    if (attested := facts.attests["accuracy"]) is None:
        if not facts.attests["labellings"]["labelled"]:
            return []
        return [f"{_labellings_drawn(facts)}; NONE WAS LABELLED BY A SIGNED-IN PERSON AND NONE IS ATTESTED"]
    found, labeller = [], attested["labeller"]
    if facts.attests["labellings"]["drawn"] > 1:
        found.append(f"{_labellings_drawn(facts)}; THE NEWEST A PERSON LABELLED IS ATTESTED")
    independent = ": THE LABELS ARE NOT INDEPENDENT OF THE RUN"
    if labeller["started_run"]:
        found.append(f"THE LABELLER STARTED RUN {facts.run['run_id']}{independent}")
    if labeller["spec_author"]:
        found.append(f"THE LABELLER WROTE SPEC APPROVAL {facts.attests['spec_approval_id']}{independent}")
    if (d := attested["discrepant"]) is not None:
        undecidable, unreviewed = d["labels"]["UNDECIDABLE"], d["reasons"].get("NOT_REVIEWED", 0)
        if unreviewed or (undecidable and undecidable * 20 >= d["uniform"]):
            found.append(
                f"{undecidable:,} OF {d['uniform']:,} SAMPLED DISCREPANCIES ARE UNDECIDABLE, {unreviewed:,}"
                " OF THEM NOT REVIEWED"
            )
    if (side := attested["truth"]["names_side"]) is not None:
        role = facts.spec.sides[Side(side)].role.upper()
        found.append(
            f"THE TRUTH SOURCE NAMES SIDE {side.upper()} ({role}): LABELS RESTING ON A COMPARED SIDE ARE"
            " CIRCULAR AND MEASURE NOTHING"
        )
    if attested["unlocatable"]:
        found.append(
            "THE KEY IS TOKENISED AND NO PATH WAS SHOWN IN CLEAR: LABELLERS COULD NOT FIND RECORDS IN THE"
            " TRUTH SOURCE"
        )
    return found


def _audit_lines(facts: _Facts) -> list[str]:
    head = facts.head
    lines = [f"head: row {head['seq']} {head['hash']}"]
    if facts.upgrade:
        lines.append(
            f"rows 1 to {facts.upgrade} were chained by 009 when it applied, after their SIEM lines (rows"
            " written before SIEM lines existed, migration 005, have none)"
        )
    lines += [
        f"{len(facts.segment)} row(s) of these objects, each from its first: {', '.join(facts.objects)}",
        "They are unaltered and complete only as checked against your SIEM (step 7).",
    ]
    return lines


def _retention_line(facts: _Facts) -> str:
    kept = facts.retention
    held = f", or later while run {facts.run['run_id']} is held" if kept["held"] else ""
    return (
        f"per-key detail stays in the store until {kept['per_key_detail_until']}{held}; the stored pack is"
        " deleted when the cycle is purged: archive this file"
    )


# -- the raw-value gate -------------------------------------------------------------------------


def raw_values(spec: CompiledSpec, files: Mapping[str, bytes]) -> list[str]:
    """Where each raw key-shaped value of the spec in the pack's text files is (``rawvalues``), the spec's
    first: a spec path (``known_diffs[2].when``), ``audit row N``, else the file and where in it. Searched:
    the spec's strings and member names but ``apiVersion`` and ``key.pattern``, its inline integer keys
    and its stratum buckets' numbers; the other JSON files' strings that are not numbers (the builder names
    their members and writes counts, rates and spec ids as text) but the manifest's fixed ones; CSV cells
    that are not numbers; each audit row's strings but its object (one of the pack's). Not JSON numbers
    (counts, times, row numbers), nor VERIFY.txt (fixed text, ids and row numbers) and the certificate
    (built from the same facts). Empty for ``business_code`` keys; rawvalues.EmptyPattern for a key pattern
    that cannot tell a key."""
    search = rawvalues.searcher(spec)
    if search is None:
        return []
    found: list[str] = []
    first = ("facts/spec.json", "facts/attests.json")
    for path in (*first, *sorted(set(files) - set(first))):
        if path not in files or not path.endswith((".json", ".csv", ".tsv")):
            continue
        text = files[path].decode()
        for where, value in _texts(path, text):
            found += [where] * search(value)
    return found


def _texts(path: str, text: str) -> Iterator[tuple[str, str]]:
    """The searchable strings of a text file, each with where it is."""
    if path == "facts/spec.json":
        for at, value in _strings(json.loads(text, parse_float=Decimal), spec=True):  # numbers as written
            if at not in (("apiVersion",), ("key", "pattern")):  # a constant, and the gate's own pattern
                yield _named(at), value
    elif path.endswith(".json"):
        for at, value in _strings(json.loads(text)):
            if not _NUMBER.fullmatch(value) and not (path == "manifest.json" and at in _FIXED):
                yield f"{path} {_named(at)}".rstrip(), value
    elif path.endswith(".csv"):
        for number, cells in enumerate(csv.reader(io.StringIO(text, newline="")), 1):
            for cell in cells:
                if not _NUMBER.fullmatch(cell):
                    yield f"{path} line {number}", cell
    else:  # newline-terminated rows, split at newlines only: a text may hold U+2028, U+2029 or U+0085
        for line in text.split("\n")[:-1]:
            seq, _, _, _, _, row = line.split("\t", 5)
            for at, value in _strings(json.loads(row)):
                if at != ("object",):
                    yield f"audit row {seq}", value


def _strings(
    value: Any, at: tuple[str | int, ...] = (), *, spec: bool = False
) -> Iterator[tuple[tuple[str | int, ...], str]]:
    """A JSON value's strings with their paths; ``spec``: its member names too, and as decimal text the
    integers of inline key lists (``key.source.keys``) and the numbers of a hash sample's buckets
    (``key.source.strata[*].buckets[*]``'s ``values``, ``min`` and ``max``), which match keys or columns."""
    if isinstance(value, str):
        yield at, value
    elif isinstance(value, int | Decimal) and not isinstance(value, bool):
        keyed = at[:2] == ("key", "source") and at[-2:-1] == ("keys",)
        bucketed = at[:3] == ("key", "source", "strata") and at[4:5] == ("buckets",)
        if spec and (keyed or (bucketed and at[6:7] in (("values",), ("min",), ("max",)))):
            yield at, str(value)
    elif isinstance(value, dict):
        for name, item in value.items():
            if spec:
                yield (*at, name), name
            yield from _strings(item, (*at, name), spec=spec)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _strings(item, (*at, i), spec=spec)


def _named(at: Sequence[str | int]) -> str:
    """A JSON path as the spec's members are named in Python (``knownDiffs`` -> ``known_diffs[2].when``)."""
    text = ""
    for part in at:
        if isinstance(part, int):
            text += f"[{part}]"
        else:
            name = _CAMEL.sub("_", part).lower() if _IDENTIFIER.fullmatch(part) else part
            text += f".{name}" if text else name
    return text


# -- signing, storing and reading packs ---------------------------------------------------


def seal(pack: Pack, signer: PackSigner) -> Sealed:
    """The signed ZIP: folder ``P-…/`` with the listed files, ``SHA256SUMS`` (coreutils lines in byte
    order), its signature and the signing key (SPKI PEM)."""
    sums = "".join(f"{hashlib.sha256(pack.files[p]).hexdigest()}  {p}\n" for p in sorted(pack.files)).encode()
    files = pack.files | dict(zip(UNLISTED, (sums, signer.sign(sums), signer.public_pem), strict=True))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for path in sorted(files):
            info = zipfile.ZipInfo(f"{pack.pack_id}/{path}", date_time=pack.issued_at.timetuple()[:6])
            info.external_attr = 0o644 << 16
            data = files[path]
            # A file that deflates too well is stored, so that no pack trips a verifier's 100:1 limit.
            info.compress_type = (
                zipfile.ZIP_DEFLATED if len(data) <= 50 * len(zlib.compress(data)) else zipfile.ZIP_STORED
            )
            zf.writestr(info, data)
    data = out.getvalue()
    return Sealed(
        sums=sums,
        zip=data,
        sums_sha256=hashlib.sha256(sums).hexdigest(),
        manifest_sha256=hashlib.sha256(pack.files["manifest.json"]).hexdigest(),
        zip_sha256=hashlib.sha256(data).hexdigest(),
    )


async def issue(ctx: Context, run_id: str, cycle: int, *, who: Principal) -> repo.Row:
    """Issue the evidence pack of the run's ended ``cycle`` as ``who``, a signed-in person: judged
    and built in memory, then stored in one transaction that reads the cycle FOR SHARE (no purge
    started), then the sign-off FOR SHARE (still approved, with the attests hash it was built from), and
    audits ``pack.issue`` last. The stored pack's row.

    PackRefused (409) with every problem, or when the sign-off was revoked meanwhile; PackForbidden (403)
    for anyone but a person; PackUnavailable (503); LookupError for an unknown run; CyclePurged (410),
    also when the cycle's purge started while the pack was built."""
    from deltasleuth.runner.purge import refuse_purged

    signer = available(ctx)
    if not who.person:
        raise PackForbidden("issuing a pack needs a signed-in person")
    async with ctx.pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
        if run is None:
            raise LookupError(f"run {run_id} not found")
        found, pack = await _judged(ctx, conn, run, cycle, who)
    if found:
        raise PackRefused("; ".join(found))
    assert pack is not None
    sealed = seal(pack, signer)
    detail = {
        "signoff_id": pack.signoff_id,
        "sums_sha256": sealed.sums_sha256,
        "manifest_sha256": sealed.manifest_sha256,
        "zip_sha256": sealed.zip_sha256,
        "key": signer.fingerprint,
        "audit_head": pack.head,
        # issued only under an Enterprise licence, or by an open build, which has none
        "licence_id": (pack.manifest["licence"] or {}).get("licence_id"),
    }
    async with repo.audited(ctx.pool) as (conn, audit):
        # The cycle first, as a purge takes it before it revokes the sign-off: a purge that started
        # refuses; one starting meanwhile waits for this transaction, then deletes this pack's zip too.
        await refuse_purged(conn, run_id, cycle, lock=True)
        signoff = await repo.signoff_for_pack(conn, pack.signoff_id)
        if signoff is None or (signoff["status"], signoff["attests_sha256"]) != (
            "approved",
            pack.attests_sha256,
        ):
            raise PackRefused(f"sign-off {pack.signoff_id} was revoked meanwhile")
        row = await repo.insert_pack(
            conn,
            {
                "pack_id": pack.pack_id,
                "run_id": run_id,
                "cycle": cycle,
                "signoff_id": pack.signoff_id,
                "adjudication_id": pack.adjudication_id,
                "manifest": pack.manifest,
                "sums_sha256": sealed.sums_sha256,
                "zip_sha256": sealed.zip_sha256,
                "key_fingerprint": signer.fingerprint,
                "audit_seq": pack.head["seq"],
                "issued_by": who.actor,
                "issued_at": pack.issued_at,
            },
            sealed.zip,
        )
        await audit(who, "pack.issue", pack.pack_id, detail)
    return row


async def stored(ctx: Context, pack_id: str) -> tuple[repo.Row, bytes]:
    """The pack and its stored ZIP; LookupError for an unknown pack, CyclePurged (410) once its cycle's
    purge has started, which deletes the ZIP (copies downloaded before still verify)."""
    from deltasleuth.runner.purge import CyclePurged

    async with ctx.pool.connection() as conn:
        data = await repo.pack_zip(conn, pack_id)  # first: a purge deleting it has started before the rest
        pack = await repo.get_pack(conn, pack_id)
        ended = pack and await repo.get_cycle(conn, pack["run_id"], pack["cycle"])
    if pack is None:
        raise LookupError(f"pack {pack_id} not found")
    purged = pack["purged_at"] or ended["purging_at"]  # type: ignore[index]
    if data is None or purged is not None:
        raise CyclePurged(
            f"pack {pack_id} was purged with cycle {pack['cycle']} on {purged:%Y-%m-%d}: its stored copy is"
            " deleted; copies downloaded before still verify"
        )
    return pack, data


async def downloaded(ctx: Context, who: Principal, pack: Mapping[str, Any]) -> None:
    """Audit ``pack.download`` of the pack, in its own transaction."""
    async with repo.audited(ctx.pool) as (_, audit):
        await audit(who, "pack.download", pack["pack_id"], {"run_id": pack["run_id"], "cycle": pack["cycle"]})


def _utc(at: datetime) -> str:
    return f"{at.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}"


async def revocations(ctx: Context, signer: PackSigner) -> dict[str, bytes]:
    """``revoked.txt`` (every revoked pack of every key used, between its header naming the current key
    and ``# end``), its signature and the current key's PEM."""
    async with ctx.pool.connection() as conn:
        revoked = await repo.revoked_packs(conn)
    lines = [
        REVOCATIONS.format(fp=signer.fingerprint, at=_utc(datetime.now(UTC))),
        *(
            f"{r['pack_id']} {r['sums_sha256']} {_utc(r['revoked_at'])} {r['key_fingerprint']}"
            for r in revoked
        ),
        "# end",
    ]
    text = ("\n".join(lines) + "\n").encode()
    return {"revoked.txt": text, "revoked.txt.sig": signer.sign(text), "signing-key.pem": signer.public_pem}


def key_files(installation: str, signer: PackSigner) -> dict[str, bytes]:
    """What the key register files at key generation: ``fingerprint.txt`` and ``procedure.txt``."""
    fingerprint = f"installation: {installation}\nfingerprint: {signer.fingerprint}\n"
    return {"fingerprint.txt": fingerprint.encode(), "procedure.txt": PROCEDURE.encode()}
