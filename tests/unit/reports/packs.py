"""A signed-off census or sampled cycle written as the app writes it, one whose sign-off attests a
labelling, the pack issued from it, and the pack's own VERIFY.txt run step by step with ``sh``."""

from __future__ import annotations

import hashlib
import io
import os
import re
import shlex
import shutil
import subprocess
import zipfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from deltasleuth import approvals
from deltasleuth.auth.principal import WORKER, Principal, expand
from deltasleuth.connectors import tables as plans
from deltasleuth.context import Context
from deltasleuth.core.canonical.encoding import sha256_hex
from deltasleuth.core.spec import compile_spec
from deltasleuth.core.types import CompiledSpec, Confirmation
from deltasleuth.reports import accuracy, pack
from deltasleuth.reports.adjudicate import store_draw, store_score
from deltasleuth.reports.csv import parse_csv, render_csv
from deltasleuth.runner import census, sample
from deltasleuth.security.signing import PackSigner
from deltasleuth.security.tokenise import mask_key
from deltasleuth.store import repo
from deltasleuth.store.chain import chain_hash
from tests.conftest import licensed_once
from tests.unit.store.four_eyes import drawn, sampled

from .conftest import SPEC_YAML, TARGETS, Sample, compare_key, doc, store_counters, store_root_causes

LISTING = """\
  source:
    kind: paginated_endpoint
    target: svc-a
    request: {method: GET, path: /list}
    pagination: {kind: cursor, itemsPath: "$.items", cursorPath: "$.next", cursorParam: cursor}
    keyPath: "$.id"
"""
RAW_KEY = "0712345678"  # a raw account number: never in a pack
CLOSE_NOTE = "segment mapping fixed in release 4.2 (CHG-118)"  # what frank recorded closing a failed draw
RECHECKED = Confirmation.RECHECKED  # every result confirmed, so that a live cycle owes no recheck
# Prose lines of the procedure's steps, indented like their commands.
_PROSE = re.compile(
    r"^   (Line 2 |one object, |up to |lines to |Rows up to |field by field|\(at as |covers them"
    r"|The SIEM also )|^ {20,}\("
)


def listed_spec(pii: bool = False, **changes: Any) -> CompiledSpec:
    """The unit spec with its keys listed by svc-a, side a's target (a census of side a); ``pii``: keys of
    class identifier, ten digits, tokenised everywhere."""
    text = SPEC_YAML.replace("  source: {kind: list, keys: [K001]}\n", LISTING)
    raw = yaml.safe_load(text)
    if pii:
        raw["key"] |= {"pattern": "^[0-9]{10}$", "class": "identifier"}
        raw["security"]["clear"] = ["$.status", "$.tags[*]"]
    raw.update(changes)
    return compile_spec(raw, targets=TARGETS)


def samples(keys: int = 6, pii: bool = False) -> dict[str, tuple[Sample, Sample]]:
    """``keys`` keys whose two sides agree."""
    names = [f"07123456{i:02d}" if pii else f"K{i:03d}" for i in range(1, keys + 1)]
    return {key: (doc(key), doc(key)) for key in names}


@dataclass(frozen=True)
class Signed:
    run_id: str
    spec_id: int
    spec: CompiledSpec
    spec_approval_id: str
    signoff_id: str | None
    people: dict[str, Principal]
    adjudication_id: str | None


@dataclass(frozen=True)
class Sampled(Signed):
    draw_id: str


async def signed_in(conn: repo.Conn, name: str, role: str) -> Principal:
    """A person signed in through the IdP a minute ago, whose sign-in is audited ``auth.login``."""
    roles = expand([role])
    user = await repo.upsert_user(conn, "https://idp.test", name, name, None, roles, signed_in=True)
    at = datetime.now(UTC)
    session = await repo.create_session(
        conn, secret_hash=os.urandom(32), actor=f"user:{user['user_id']}", name=name, roles=roles,
        max_hours=8, user_id=user["user_id"], roles_sha256="0" * 64, auth_time=at,
    )  # fmt: skip
    who = Principal(
        f"user:{user['user_id']}", name, roles, request_id="Q-" + os.urandom(8).hex(),
        session_id=session["session_id"], auth_time=at, client_addr="10.1.2.3",
    )  # fmt: skip
    await repo.insert_audit(conn, who, "auth.login", session["session_id"], {"method": "oidc"})
    return who


async def grant(
    conn: repo.Conn, approval_id: str, who: Principal, requested: Mapping[str, Any], *, first: bool = False
) -> repo.Row:
    """``who``'s approval of the request, audited as the decision route audits it: the only step, or the
    last of two; ``first``: the first of two."""
    row = await repo.decide_approval(conn, approval_id, who.actor, approve=True, first=first)
    assert row is not None and row["status"] == ("requested" if first else "approved")
    step, of = (1, 2) if first else (2, 2) if row["first_by"] else (1, 1)
    detail = dict(requested) | {"step": step, "of": of, "authors": row["authors"]}
    await repo.insert_audit(
        conn, who, "approval.grant", approval_id, detail | {"auth_time": who.auth_time.isoformat()}
    )  # type: ignore[union-attr]
    return row


async def signed_cycle(
    ctx: Context,
    *,
    spec: CompiledSpec | None = None,
    pairs: Mapping[str, tuple[Sample, Sample]] | None = None,
    adjudicate: bool = False,
    disagree: bool = False,
    notes: str = "",
    headers: Mapping[str, str] | None = None,
    sign: bool = True,
    scan: Mapping[str, Any] | None = None,
    tier: str | None = "enterprise",
    mode: str = "batch",
    schedule: Mapping[str, Any] | None = None,
    tables: Mapping[str, Any] | None = None,
    pinned: Mapping[str, Any] | None = None,
) -> Signed:
    """Cycle 1 of a batch run over ``pairs`` (default: six agreeing keys), its keys listed by side a's
    target, run under bob's spec version that carol approved, started by olga, ended and (``sign``) signed
    off: olga asked, erin approved. ``adjudicate``: olga drew a sheet of 50 before the request, frank scored
    it agreeing (``disagree``: but on its first row), with ``notes`` typed on every row. Each response
    carries ``headers``. ``scan``: a table scan's state instead of the listing, the run reading table
    ``public.items`` on each side. In a vendor build (``vendor_keys``) the run is made under a licence of
    ``tier``, installed unless the store names one (None: none, so the run is an unlicensed evaluation's
    unless the store names a licence); in the open build it records none.
    ``mode="soak"``: a running soak whose cycle 1 read every key, its boundary (``finish_cycle``), which
    ends the cycle, and its sign-off (``sign`` False) left to the caller. ``schedule``: the run is a
    schedule's firing, its ``params["schedule"]``: created by ``system:worker``
    and audited as the worker audits one, so its sign-off takes two approvers, carol then erin. ``tables``:
    the scan's stored plan in place of ``public.items`` twice, and ``pinned`` a file side's pins, sealed as
    ``create_run`` seals them."""
    spec = spec or listed_spec()
    pairs = pairs or samples()
    record = (await licensed_once(ctx, tier)).record()
    async with ctx.pool.connection() as conn:
        people = {
            name: await signed_in(conn, name, role)
            for name, role in (("bob", "spec_author"), ("carol", "rule_approver"), ("erin", "rule_approver"),
                               ("olga", "operator"), ("frank", "operator"))
        }  # fmt: skip
        bob, carol, olga = people["bob"], people["carol"], people["olga"]
        spec_id = await repo.save_spec(conn, spec, created_by=bob.actor)
        await repo.insert_audit(conn, bob, "spec.create", str(spec_id), {"content_hash": spec.content_hash})
        needs = approvals.needs(spec)
        request = await repo.create_approval(conn, "spec", spec_id, bob.actor, scopes=needs)
        assert request is not None
        spec_approval_id = request["approval_id"]
        asked = {"kind": "spec", "spec_id": spec_id, "content_hash": spec.content_hash, "scopes": needs}
        await repo.insert_audit(conn, bob, "approval.request", spec_approval_id, asked)
        await grant(conn, spec_approval_id, carol, asked)
        run_id = repo.new_run_id()
        kek, wrapped, sealer = ctx.new_sealer(run_id)
        keys = {key: mask_key(key, spec, ctx.tokeniser) for key in pairs}
        plan = {"schema": "public", "name": "items", "keys": ["id"], "columns": {"name": "name"},
                "where": None}  # fmt: skip
        params = {
            "keys_source": "paginated_endpoint" if scan is None else "table_scan", "samples_dir": None,
            "soak": {"rolling": False} if mode == "soak" else None, "capabilities": ctx.declared(spec),
            "tables": None if scan is None else {"a": plan, "b": plan, "only_a": [], "only_b": []},
            "approval": {"needs": needs, "mode": "oidc"}, "licence": record,
        }  # fmt: skip
        params |= {"tables": dict(tables)} if tables else {}
        params |= {"pinned": plans.sealed(sealer, pinned)} if pinned else {}
        params |= {"schedule": dict(schedule)} if schedule else {}
        starter = WORKER if schedule else olga
        stored = await repo.create_run(
            conn, spec_id, mode, sealer.seal_keys(keys), params, scan, run_id=run_id,
            created_by=starter.actor, retention="standard", data_key=(kek, wrapped),
            approval_id=spec_approval_id,
        )  # fmt: skip
        if scan is None:
            at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            walk = {"basis": "listing", "target": "svc-a", "items": len(pairs), "first_at": at, "last_at": at}
            await repo.set_census(conn, run_id, census.listed(spec, walk, 1))  # type: ignore[arg-type]
        detail = {"spec_id": spec_id, "mode": mode, "keys": len(keys), "file_mode": False}
        detail |= {"approval_id": spec_approval_id, "approval": params["approval"], "licence": record}
        detail["plan_sha256"] = sha256_hex(approvals.plan(stored))
        detail |= {"schedule": dict(schedule)} if schedule else {}
        await repo.insert_audit(conn, starter, "create_run", run_id, detail)
        if schedule:
            fired = {name: schedule[name] for name in ("due", "revision", "time_zone")}
            fired |= {"run_id": run_id, "spec_id": spec_id, "spec_name": spec.name, "mode": mode}
            await repo.insert_audit(
                conn, WORKER, "schedule.fire", schedule["schedule_id"], fired | {"late_s": 4, "next": None}
            )
        results = [
            await compare_key(
                ctx, conn, spec, run_id, key, pair, n=n, headers=headers, confirmation=RECHECKED
            )
            for n, (key, pair) in enumerate(pairs.items())
        ]
        await store_root_causes(ctx, conn, spec, run_id, 1, results)
        await store_counters(conn, spec, run_id, 1, True)
        if mode == "soak":  # every key read, as its worker marks them
            for masked in keys.values():
                await repo.complete_inline(conn, run_id, 1, masked)
            await repo.set_run_status(conn, run_id, "running")
        else:
            await repo.set_run_status(conn, run_id, "completed")
            await repo.end_cycle(conn, run_id)
            await repo.insert_audit(
                conn, olga, "run.finished", run_id, {"status": "completed", "reason": None}
            )
    adjudication_id = None
    if adjudicate:
        drawn = await store_draw(ctx, run_id, 1, who=olga)
        header, rows = parse_csv(drawn["sheet"].encode())
        for row in rows:
            row |= {"reviewer_verdict": row["engine_verdict"], "reviewer_notes": notes}
        if disagree:
            rows[0]["reviewer_verdict"] = "DIFFERENT" if rows[0]["engine_verdict"] == "MATCH" else "MATCH"
        await store_score(
            ctx, drawn["adjudication_id"], render_csv(header, rows).encode(), who=people["frank"]
        )
        adjudication_id = drawn["adjudication_id"]
    first = carol if schedule else None
    signoff_id = await sign_off(ctx, run_id, 1, spec, olga, people["erin"], first=first) if sign else None
    return Signed(run_id, spec_id, spec, spec_approval_id, signoff_id, people, adjudication_id)


async def sampled_cycle(
    ctx: Context,
    *,
    spec: CompiledSpec | None = None,
    earlier: bool = False,
    sign: bool = True,
    **options: Any,
) -> Sampled:
    """Cycle 1 of a batch run drawing a sample of side a's customers (``four_eyes.sampled()``: its two
    segments of three, each a census stratum, unless ``spec`` says otherwise), run under bob's spec version
    that carol approved, started by olga (in a vendor build, under an Enterprise licence), its draw
    committed as the worker commits one, each key compared, ended with its judgement (``options``:
    ``four_eyes.drawn``'s) and (``sign``) signed off: olga asked, erin approved. ``earlier``: a draw of the
    same customers under customer-profile-2 failed before it, and frank closed it with ``CLOSE_NOTE``."""
    spec = spec or sampled()
    record = (await licensed_once(ctx)).record()
    async with ctx.pool.connection() as conn:
        people = {
            name: await signed_in(conn, name, role)
            for name, role in (("bob", "spec_author"), ("carol", "rule_approver"), ("erin", "rule_approver"),
                               ("olga", "operator"), ("frank", "operator"))
        }  # fmt: skip
        bob, olga = people["bob"], people["olga"]
        spec_id = await repo.save_spec(conn, spec, created_by=bob.actor)
        await repo.insert_audit(conn, bob, "spec.create", str(spec_id), {"content_hash": spec.content_hash})
        needs = approvals.needs(spec)
        request = await repo.create_approval(conn, "spec", spec_id, bob.actor, scopes=needs)
        assert request is not None
        spec_approval_id = request["approval_id"]
        asked = {"kind": "spec", "spec_id": spec_id, "content_hash": spec.content_hash, "scopes": needs}
        await repo.insert_audit(conn, bob, "approval.request", spec_approval_id, asked)
        await grant(conn, spec_approval_id, people["carol"], asked)
        if earlier:
            other = sampled("customer-profile-2")
            other_id = await repo.save_spec(conn, other, created_by=bob.actor)
            await drawn(conn, other_id, other, olga, discrepant=["C001"])
    if earlier:  # closed before the next draw, whose chain row follows the close's
        async with repo.audited(ctx.pool) as (conn, audit):
            await sample.close(conn, audit, "customer-profile-2", CLOSE_NOTE, people["frank"])
    async with ctx.pool.connection() as conn:
        run_id, draw_id = await drawn(
            conn, spec_id, spec, olga, approval_id=spec_approval_id, record=record, **options
        )
    signoff_id = await sign_off(ctx, run_id, 1, spec, olga, people["erin"]) if sign else None
    return Sampled(run_id, spec_id, spec, spec_approval_id, signoff_id, people, None, draw_id)


TRUTH = {"source": "CBS-PROD.account_master", "as_of": "2026-10-01"}
Label = tuple[str, str, str]  # label, basis, reason
RIGHT: Label = ("B_RIGHT", "SYSTEM_OF_RECORD", "")


def labelled_pairs(different: int = 8, **options: Any) -> dict[str, tuple[Sample, Sample]]:
    """Twenty agreeing keys and ``different`` whose status differs (one root cause, HIGH)."""
    pairs = samples(20, **options)
    for key in list(pairs)[:different]:
        pairs[key] = (doc(key, status="A"), doc(key, status="B"))
    return pairs


def labelled_sheet(sheet: str, labels: Mapping[str, Label] | None = None, notes: str = "") -> bytes:
    """The drawn sheet filled: side b right on the system of record, but the items of ``labels``."""
    header, rows = parse_csv(sheet.encode())
    for row in rows:
        label, basis, reason = (labels or {}).get(row["item"], RIGHT)
        row |= {"label": label, "basis": basis, "reason": reason, "notes": notes}
    return render_csv(header, rows).encode()


async def labelled_cycle(
    ctx: Context,
    *,
    by: str = "frank",
    truth: Mapping[str, str] = TRUTH,
    labels: Mapping[str, Label] | None = None,
    blank: bool = False,
    sign: bool = True,
    **options: Any,
) -> tuple[Signed, repo.Row]:
    """``signed_cycle`` over ``labelled_pairs`` (``options``: its own), with a labelling olga drew (five
    discrepant items and four agreed keys against ``truth``) and ``by`` labelled (``labelled_sheet``) before
    olga asked for the sign-off, which erin approved (``sign``); ``blank``: a labelling drawn and left
    unlabelled before it. The cycle and the labelling as ``repo.get_labelling`` shows it."""
    options.setdefault("pairs", labelled_pairs())
    signed = await signed_cycle(ctx, sign=False, **options)
    olga, sizes = signed.people["olga"], {"size": 5, "per_cause": 0, "agreed": 4}
    acknowledged = {"acknowledge_unlocatable": accuracy._unlocatable(signed.spec)}
    if blank:
        await accuracy.store_draw(ctx, signed.run_id, 1, who=olga, size=1, truth=truth, **acknowledged)
    drawn, _ = await accuracy.store_draw(
        ctx, signed.run_id, 1, who=olga, truth=truth, **sizes, **acknowledged
    )
    upload = labelled_sheet(drawn["sheet"], labels, "called the branch")
    await accuracy.store_labels(ctx, drawn["labelling_id"], upload, who=signed.people[by])
    if sign:
        signed = replace(
            signed, signoff_id=await sign_off(ctx, signed.run_id, 1, signed.spec, olga, signed.people["erin"])
        )
    async with ctx.pool.connection() as conn:
        row = await repo.get_labelling(conn, drawn["labelling_id"])
    assert row is not None
    return signed, row


Attests = Callable[[repo.Conn, repo.Row, int, CompiledSpec], Awaitable[dict[str, Any]]]


async def sign_off(
    ctx: Context,
    run_id: str,
    cycle: int,
    spec: CompiledSpec,
    requester: Principal,
    approver: Principal,
    *,
    attests: Attests = approvals.attests,
    first: Principal | None = None,
) -> str:
    """The cycle's sign-off, requested and approved (holding the run) as the routes do; a sampled cycle's
    grant records the later draws of its population. ``attests``: what builds the
    facts it attests (an earlier format's builder: a request made before a migration). ``first``: who takes
    the first of two steps, where the run's start has no accountable person."""
    async with ctx.pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
        assert run is not None
        facts = await attests(conn, run, cycle, spec)
        sha256 = approvals.attests_sha256(facts)
        row = await repo.create_approval(
            conn, "signoff", run["spec_id"], requester.actor, run_id=run_id, cycle=cycle, attests=facts,
            attests_sha256=sha256,
        )  # fmt: skip
        assert row is not None
        asked = {"kind": "signoff", "spec_id": run["spec_id"], "run_id": run_id, "cycle": cycle}
        await repo.insert_audit(
            conn, requester, "approval.request", row["approval_id"], asked | {"attests_sha256": sha256}
        )
        if first is not None:
            await grant(conn, row["approval_id"], first, asked | {"attests_sha256": sha256}, first=True)
        later = {"later_draws": await sample.later_draws(conn, run_id, cycle)} if facts["sample"] else {}
        await grant(conn, row["approval_id"], approver, asked | {"attests_sha256": sha256} | later)
        hold = f"{approvals.SIGNED_OFF}{row['approval_id']}"
        if await repo.set_hold(conn, run_id, hold, approver.actor) is not None:
            await repo.insert_audit(conn, approver, "retention.hold", run_id, {"reason": hold})
    return row["approval_id"]


def unzip(data: bytes, into: Path) -> Path:
    """The pack's folder, unzipped into ``into``."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(into)
        (folder,) = {name.split("/")[0] for name in zf.namelist()}
    return into / folder


def files_of(data: bytes) -> dict[str, bytes]:
    """The pack's files by path inside its folder."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {name.split("/", 1)[1]: zf.read(name) for name in zf.namelist()}


def rezip(folder: Path, into: Path) -> Path:
    """The folder zipped again, as a pack would be."""
    path = into / f"{folder.name}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(p for p in folder.rglob("*") if p.is_file()):
            zf.write(file, f"{folder.name}/{file.relative_to(folder)}")
    return path


def resign(folder: Path, signer: PackSigner) -> None:
    """Rewrite SHA256SUMS over the folder's listed files and sign it with ``signer``, as a holder of the
    key who edited the files would; a file added to the folder is listed."""
    names = sorted(
        str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file() and p.name not in pack.UNLISTED
    )
    sums = "".join(f"{hashlib.sha256((folder / n).read_bytes()).hexdigest()}  {n}\n" for n in names).encode()
    (folder / "SHA256SUMS").write_bytes(sums)
    (folder / "SHA256SUMS.sig").write_bytes(signer.sign(sums))
    (folder / "signing-key.pem").write_bytes(signer.public_pem)


def chain_rows(folder: Path) -> list[list[str]]:
    """audit/chain.tsv's rows, split at newlines only, as sh reads them (a text may hold U+2028)."""
    return [
        line.split("\t", 5) for line in (folder / "audit/chain.tsv").read_bytes().decode().split("\n")[:-1]
    ]


def write_chain(folder: Path, rows: list[list[str]], *, rehash: bool = True) -> None:
    """audit/chain.tsv from ``rows``; ``rehash``: each row's hash and the links after it recomputed, as the
    database owner rewriting the chain consistently would."""
    if rehash:
        last: dict[str, str] = {}
        hashes: dict[int, str] = {}
        for row in rows:
            seq = int(row[0])
            row[1] = hashes.get(seq - 1, row[1])
            row[2] = last.get(row[4], "0" * 64)
            row[3] = chain_hash(row[1], row[2], row[5])
            last[row[4]] = hashes[seq] = row[3]
    (folder / "audit/chain.tsv").write_text("".join("\t".join(row) + "\n" for row in rows))


def tools() -> None:
    """Skip without openssl, sha256sum and shasum; fail instead where they must be (DS_REQUIRE_TOOLS=1)."""
    missing = [tool for tool in ("openssl", "sha256sum", "shasum") if shutil.which(tool) is None]
    if missing:
        (pytest.fail if os.environ.get("DS_REQUIRE_TOOLS") else pytest.skip)(f"missing {', '.join(missing)}")


def steps(verify: str, cd: Path | None = None) -> dict[int, str]:
    """Each numbered step of VERIFY.txt as a shell script: its command lines, each without its ``  ->
    expected output`` note, a later step's after step 0's commands that read line 2's values (all but its
    diff, run in the pack's folder) and, with ``cd``, a ``cd`` to where the step runs."""
    found: dict[int, list[str]] = {}
    number = None
    for line in verify.split("\n")[2:]:
        if match := re.match(r"(\d)\. ", line):
            number = int(match[1])
            found[number] = []
        elif number is not None and line.startswith("   ") and not _PROSE.match(line):
            found[number].append(line.split("  -> ")[0])
    reads = [*found[0][1:], *([f"cd {shlex.quote(str(cd))}"] if cd else [])]
    return {n: "\n".join(commands if n == 0 else [*reads, *commands]) for n, commands in found.items()}


@dataclass(frozen=True)
class Ran:
    code: int
    out: list[str]
    err: str


def run_step(folder: Path, number: int, *, shasum: bool = False, cwd: Path | None = None) -> Ran:
    """Step ``number`` of the folder's own VERIFY.txt, run with ``sh`` in the folder (or ``cwd``, once
    step 0 read line 2 in the folder); ``shasum``: with the procedure's ``sha256sum`` function over
    ``shasum -a 256``."""
    script = steps((folder / "VERIFY.txt").read_text(), cwd)[number]
    if shasum:
        script = 'sha256sum() { shasum -a 256 "$@"; }\n' + script
    done = subprocess.run(["sh", "-c", script], cwd=folder, capture_output=True, text=True, check=False)
    return Ran(done.returncode, done.stdout.splitlines(), done.stderr)


async def siem_export(ctx: Context, folder: Path) -> None:
    """``../siem.tsv`` as step 7 asks for it: a SIEM that ingested every row at write time, exported for the
    pack's objects up to its head, rows chained after the upgrade as ``seq<TAB>hash``."""
    variables = dict(part.split("=") for part in (folder / "VERIFY.txt").read_text().split("\n")[1].split())
    objects = sorted({row[4] for row in chain_rows(folder)})
    async with ctx.pool.connection() as conn:
        cur = await conn.execute(
            """SELECT seq, hash FROM ds.audit_chain WHERE object = ANY(%s) AND seq > %s AND seq <= %s
               ORDER BY seq""",
            (objects, int(variables["UPGRADE"]), int(variables["HEAD"])),
        )
        rows = await cur.fetchall()
    (folder.parent / "siem.tsv").write_text("".join(f"{seq}\t{digest}\n" for seq, digest in rows))


def file_procedure(folder: Path, signer: PackSigner) -> None:
    """The procedure filed with the key register, where step 0 finds it."""
    (folder.parent / "FILED-PROCEDURE.txt").write_bytes(pack.key_files("test-1", signer)["procedure.txt"])
