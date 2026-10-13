"""A pack checked offline (docs/mvp/PACK.md §3.3, §7.3, §9): its own VERIFY.txt run with ``sh``, with
``sha256sum`` and then ``shasum -a 256`` (and macOS's LibreSSL where it is installed), and ``ds packs
verify``. The untouched pack passes; each tamper fails the step the procedure says catches it; the
revocation list and the zip limits refuse what they must. A sampled pack also passes its certificate's hand
check, which catches a draw row or a salt that does not match (docs/mvp/SAMPLING.md §11.4); a pack
attesting a labelling passes its own, which catches its labels, its score or its accuracy.label row changed
(docs/mvp/ACCURACY.md §9)."""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
import subprocess
import zipfile
from collections.abc import Callable
from functools import partial
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from typer.testing import CliRunner

from deltasleuth import approvals
from deltasleuth.cli.main import app
from deltasleuth.context import Context
from deltasleuth.core import sampling
from deltasleuth.reports import pack, pack_verify
from deltasleuth.runner import purge
from deltasleuth.security.signing import PackSigner
from deltasleuth.store import chain, repo

from .packs import (
    Ran,
    Signed,
    chain_rows,
    file_procedure,
    labelled_cycle,
    listed_spec,
    resign,
    rezip,
    run_step,
    sampled_cycle,
    samples,
    siem_export,
    sign_off,
    signed_cycle,
    signed_in,
    steps,
    tools,
    unzip,
    write_chain,
)

pytestmark = pytest.mark.db
LIBRESSL = Path("/usr/bin/openssl")  # macOS's: the procedure must work with it too
Case = tuple[Callable[[Path], None], int, Callable[[Ran], bool], int, str]


def _libressl() -> bool:
    if not LIBRESSL.exists():
        return False
    found = subprocess.run([LIBRESSL, "version"], capture_output=True, text=True, check=False)
    return found.stdout.startswith("LibreSSL")


VARIANTS = ["sha256sum", "shasum", *(["libressl"] if _libressl() else [])]


def run_with(folder: Path, number: int, variant: str, cwd: Path | None = None) -> Ran:
    """A step under ``sha256sum``, ``shasum`` (the procedure's function) or ``libressl`` (both shims)."""
    if variant != "libressl":
        return run_step(folder, number, shasum=variant == "shasum", cwd=cwd)
    script = f'openssl() {{ {LIBRESSL} "$@"; }}\nsha256sum() {{ shasum -a 256 "$@"; }}\n'
    script += steps((folder / "VERIFY.txt").read_text(), cwd)[number]
    done = subprocess.run(["sh", "-c", script], cwd=folder, capture_output=True, text=True, check=False)
    return Ran(done.returncode, done.stdout.splitlines(), done.stderr)


async def _issued(
    ctx: Context, tmp_path: Path, *, sampled: bool = False
) -> tuple[Signed, repo.Row, Path, Path]:
    """A pack of an adjudicated cycle (``sampled``: of a sampled cycle after a closed failed draw): its row,
    its ZIP as ``tmp_path/pack.zip``, and its folder unzipped in ``tmp_path/pristine`` beside the register's
    procedure and the SIEM's export."""
    tools()
    signed = await (sampled_cycle(ctx, earlier=True) if sampled else signed_cycle(ctx, adjudicate=True))
    issued = await pack.issue(ctx, signed.run_id, 1, who=signed.people["olga"])
    _, data = await pack.stored(ctx, issued["pack_id"])
    (tmp_path / "pack.zip").write_bytes(data)
    folder = unzip(data, tmp_path / "pristine")
    file_procedure(folder, ctx.signer)  # type: ignore[arg-type]
    await siem_export(ctx, folder)
    return signed, issued, tmp_path / "pack.zip", folder


async def _revocations(ctx: Context, into: Path) -> Path:
    into.mkdir(parents=True, exist_ok=True)
    for name, content in (await pack.revocations(ctx, ctx.signer)).items():  # type: ignore[arg-type]
        (into / name).write_bytes(content)
    return into


def _copy(folder: Path, into: Path) -> Path:
    """A copy of the unzipped pack, with the filed procedure and the SIEM export beside it."""
    shutil.copytree(folder.parent, into)
    return into / folder.name


def verify(path: Path, ctx: Context, revocations: Path | None = None) -> dict[int, str]:
    """``ds packs verify``'s steps by number: ``ok (…)``, ``FAILED (…)`` or what it shows; ``path`` is a
    ZIP, or a folder zipped again beside it."""
    zipped = path if path.suffix == ".zip" else rezip(path, path.parent)
    found = pack_verify.verify(zipped, ctx.signer.fingerprint, revocations)  # type: ignore[union-attr]
    return {step.number: step.line.split(": ", 1)[1] for step in found}


def _subject(folder: Path) -> str:
    subject = json.loads((folder / "manifest.json").read_text())["subject"]
    spec = subject["spec"]
    return (
        f"run {subject['run_id']} cycle 1; basis census; spec {spec['name']} {spec['version']} (content hash"
        f" {spec['content_hash']}); side a legacy svc-a; side b new svc-b: match it with your change record"
    )


def _step_7(folder: Path) -> str:
    variables = dict(p.split("=") for p in (folder / "VERIFY.txt").read_text().split("\n")[1].split())
    objects = ", ".join(sorted({row[4] for row in chain_rows(folder)}))
    return (
        f"compare with your SIEM's deltasleuth.audit lines up to row {variables['HEAD']} of {objects}: rows"
        f" after {variables['UPGRADE']} by seq and hash, those up to it with the line of their id, field by"
        " field (id, at, actor, actor_name, action, object, detail, request_id, session_id, client_addr; at"
        " as an instant); rows written before SIEM lines existed (migration 005) have none: only the chain"
        " covers them, from the upgrade on"
    )


# The certificate's hand check as an auditor types it: the draw's row of audit/chain.tsv ($1), then the
# SHA-256 of the text label:nonce ($2).
HAND_CHECK = """awk -F'\\t' -v s="$1" '$1 == s' audit/chain.tsv
printf '%s' "$2" | sha256sum | cut -c1-64"""


def hand_check(folder: Path, seq: int, salt: str, variant: str) -> tuple[str, str]:
    """The draw's audit text and the salt's SHA-256, under ``sha256sum`` or ``shasum -a 256``."""
    script = ("" if variant == "sha256sum" else 'sha256sum() { shasum -a 256 "$@"; }\n') + HAND_CHECK
    done = subprocess.run(
        ["sh", "-c", script, "sh", str(seq), salt], cwd=folder, capture_output=True, text=True, check=False
    )
    row, digest = done.stdout.splitlines()
    return row.split("\t", 5)[5], digest


def _flip(path: Path) -> None:
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0x01
    path.write_bytes(bytes(data))


def _two_blocks(first: PackSigner, then: PackSigner) -> bytes:
    """``first``'s PEM block with an RFC 1421 header line, then ``then``'s: a strict reader skips the first,
    OpenSSL and cryptography read it."""
    begin, rest = first.public_pem.split(b"\n", 1)
    return begin + b"\nComment: x\n\n" + rest + then.public_pem


async def test_the_untouched_pack_passes_its_own_procedure_and_ds_packs_verify(
    pack_ctx: Context, tmp_path: Path
) -> None:
    signed, issued, zipped, folder = await _issued(pack_ctx, tmp_path)
    revoked = await _revocations(pack_ctx, tmp_path / "custodian")
    fingerprint = pack_ctx.signer.fingerprint  # type: ignore[union-attr]
    sums = hashlib.sha256((folder / "SHA256SUMS").read_bytes()).hexdigest()
    listed = sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.name not in pack.UNLISTED)
    listed = [name for name in listed if (folder / name).is_file()]
    for variant in VARIANTS:
        ran = {n: run_with(folder, n, variant) for n in range(8)}
        ran[8] = run_with(folder, 8, variant, cwd=revoked)
        assert (ran[0].code, ran[0].out) == (0, []), variant
        assert ran[1].out == [f"{fingerprint}  -"], variant
        assert ran[2].out == ["Verified OK"], variant
        assert (ran[3].code, ran[3].out) == (0, [f"{name}: OK" for name in listed]), variant
        assert ran[4].out[0] == '  "subject": {' and f'    "run_id": "{signed.run_id}",' in ran[4].out
        decided = [f"{signed.signoff_id}: decided", f"{signed.spec_approval_id}: decided", "1", "1", "1"]
        assert ran[5].out == decided, variant  # the plan, then the sheet and score, each found once
        assert (ran[6].code, ran[6].out) == (0, []), variant
        assert (ran[7].code, ran[7].out) == (0, [sums]), variant  # no diff; upgrade 0: no row by content
        key, verified, head, end, count = ran[8].out
        assert (key, verified, end, count) == (f"{fingerprint}  -", "Verified OK", "# end", "0"), variant
        assert head.startswith(f"# DeltaSleuth pack revocations, key {fingerprint}, as of "), variant
        assert all(r.err == "" for r in ran.values()), variant
    as_of = head.rsplit(" ", 1)[1]
    assert verify(zipped, pack_ctx, revoked) == {
        0: "ok",
        1: f"ok (fingerprint {fingerprint})",
        2: "ok",
        3: f"ok ({len(listed)} listed file(s) unchanged, none added)",
        4: _subject(folder),
        5: f"ok (sign-off {signed.signoff_id} and spec approval {signed.spec_approval_id} decided)",
        6: f"ok ({len(chain_rows(folder))} row(s) consistent)",
        7: _step_7(folder),
        8: f"ok (not listed as of {as_of})",
    }
    result = CliRunner().invoke(app, ["packs", "verify", str(zipped), "--fingerprint", fingerprint.upper()])
    assert result.exit_code == 0
    assert result.stdout.splitlines()[-1] == "step 8 revocation: not checked: pass --revocations DIR"
    assert issued["sums_sha256"] == sums
    assert (5, "labelled accuracy") not in _steps(zipped, pack_ctx)  # it attests no labelling: no such line


async def test_line_separators_in_an_audited_reason_split_no_row_of_the_pack_or_its_siem_lines(
    pack_ctx: Context, tmp_path: Path
) -> None:
    """U+2028 in a holder's name (an IdP's display name), U+2029 in the hold's reason and U+0085, a
    control character only a direct writer such as psql can audit, raw in the audit texts and SIEM lines,
    where str.splitlines() would split the rows: the raw-value gate, the procedure, ds packs verify and
    read_siem each read them whole."""
    tools()
    signed = await signed_cycle(pack_ctx, spec=listed_spec(pii=True), pairs=samples(pii=True), sign=False)
    async with pack_ctx.pool.connection() as conn:
        dana = await signed_in(conn, "Dana\u2028Lee", "operator")
    reason = "legal hold LH-7\u2029pasted from the case system"
    await purge.hold(pack_ctx, dana, signed.run_id, reason)
    await purge.hold(pack_ctx, dana, signed.run_id, None)
    async with pack_ctx.pool.connection() as conn:
        await repo.insert_audit(conn, dana, "retention.hold", signed.run_id, {"reason": "pasted\u0085notes"})
    olga = signed.people["olga"]
    signoff_id = await sign_off(pack_ctx, signed.run_id, 1, signed.spec, olga, signed.people["erin"])
    async with pack_ctx.pool.connection() as conn:
        run = await repo.get_run(conn, signed.run_id)
        assert run is not None and await pack.problems(pack_ctx, conn, run, 1) == []
    issued = await pack.issue(pack_ctx, signed.run_id, 1, who=olga)
    _, data = await pack.stored(pack_ctx, issued["pack_id"])
    (tmp_path / "pack.zip").write_bytes(data)
    folder = unzip(data, tmp_path / "pristine")
    file_procedure(folder, pack_ctx.signer)  # type: ignore[arg-type]
    await siem_export(pack_ctx, folder)
    rows = chain_rows(folder)
    assert [sum(c in row[5] for row in rows) for c in ("Dana\u2028Lee", "\u2029", "\u0085")] == [3, 2, 1]
    ran = {n: run_step(folder, n) for n in (0, 5, 6, 7)}
    assert (ran[0].out, ran[6].out, ran[7].out) == ([], [], [issued["sums_sha256"]])
    assert ran[5].out[:2] == [f"{signoff_id}: decided", f"{signed.spec_approval_id}: decided"]
    found = verify(tmp_path / "pack.zip", pack_ctx)
    assert all(found[n].startswith("ok") for n in (0, 1, 2, 3, 5)), found
    assert found[6] == f"ok ({len(rows)} row(s) consistent)"
    ids = [json.loads(row[5])["id"] for row in rows]
    async with pack_ctx.pool.connection() as conn:
        stored = await repo.audit_rows(conn, ids)
    (tmp_path / "siem.jsonl").write_text("".join(f"{repo.siem_line(row)}\n" for row in stored))
    lines = chain.read_siem(tmp_path / "siem.jsonl")
    assert [line["id"] for line in lines] == sorted(ids)
    held = [line["detail"]["reason"] for line in lines if line["actor_name"] == "Dana\u2028Lee"]
    assert held == [reason, reason, "pasted\u0085notes"]


async def test_each_tamper_fails_the_step_that_catches_it(pack_ctx: Context, tmp_path: Path) -> None:
    signed, _, _, pristine = await _issued(pack_ctx, tmp_path)
    signer = pack_ctx.signer
    assert signer is not None
    other = PackSigner(ec.generate_private_key(ec.SECP256R1()))
    rows = chain_rows(pristine)
    grant = next(i for i, r in enumerate(rows) if r[4] == signed.signoff_id and '"approval.grant"' in r[5])
    last_hash, signoff = rows[-1][3], signed.signoff_id

    def edited_text(folder: Path) -> None:
        edited = chain_rows(folder)
        edited[grant][5] = edited[grant][5].replace('"actor_name": "erin"', '"actor_name": "eve"')
        write_chain(folder, edited, rehash=False)
        resign(folder, signer)

    def last_dropped(folder: Path) -> None:
        write_chain(folder, chain_rows(folder)[:-1], rehash=False)
        resign(folder, signer)

    def other_cycle(folder: Path) -> None:
        attests = json.loads((folder / "facts/attests.json").read_text())
        (folder / "facts/attests.json").write_text(json.dumps(attests | {"cycle": 2}, sort_keys=True))
        resign(folder, signer)

    def half_granted(folder: Path) -> None:
        edited = chain_rows(folder)
        edited[grant][5] = edited[grant][5].replace('"of": 1', '"of": 2')
        write_chain(folder, edited)  # rewritten consistently, as the database owner could before issue
        resign(folder, signer)

    def revoke_row(folder: Path) -> None:
        edited = chain_rows(folder)
        text = json.dumps({"action": "approval.revoke", "object": signoff}, separators=(", ", ": "))
        edited.append([str(int(edited[-1][0]) + 1), "", "", "", signoff, text])
        write_chain(folder, edited)
        resign(folder, signer)

    def added(folder: Path) -> None:
        (folder / "extra.txt").write_text("not listed\n")

    def forged_verify(folder: Path) -> None:
        text = (folder / "VERIFY.txt").read_text().replace("sha256sum -c SHA256SUMS", "true")
        (folder / "VERIFY.txt").write_text(text)

    def shell_in_line_2(folder: Path) -> None:  # the auditor's shell must never run it
        lines = (folder / "VERIFY.txt").read_text().split("\n")
        lines[1] += "; openssl() { echo Verified OK; }; echo injected"
        (folder / "VERIFY.txt").write_text("\n".join(lines))

    def line_before_the_header(folder: Path) -> None:
        lines = (folder / "VERIFY.txt").read_text().split("\n")
        (folder / "VERIFY.txt").write_text("\n".join([*lines[:2], "echo injected", *lines[2:]]))

    def two_key_blocks(folder: Path) -> None:  # another key signs; the register's block follows its own
        resign(folder, other)
        (folder / "signing-key.pem").write_bytes(_two_blocks(other, signer))

    def flipped_and_listed(folder: Path) -> None:
        _flip(folder / "facts/spec.json")
        digest = hashlib.sha256((folder / "facts/spec.json").read_bytes()).hexdigest()
        lines = (folder / "SHA256SUMS").read_text().splitlines()
        relisted = [
            f"{digest}  facts/spec.json" if line.endswith("  facts/spec.json") else line for line in lines
        ]
        (folder / "SHA256SUMS").write_text("".join(f"{line}\n" for line in relisted))

    undecided, not_decided = f"FAILED (sign-off {signoff} is NOT DECIDED)", f"{signoff}: NOT DECIDED"
    cases: dict[str, Case] = {
        # name: (tamper, the step that catches it, how it shows there, ds packs verify's step, its text)
        "a flipped byte": (
            lambda f: _flip(f / "facts/spec.json"), 3, lambda r: "facts/spec.json: FAILED" in r.out,
            3, "FAILED (facts/spec.json differs from its SHA256SUMS line)",
        ),
        "a flipped byte, also in its sums line": (
            flipped_and_listed, 2, lambda r: r.out != ["Verified OK"], 2, "FAILED",
        ),
        "another key": (
            partial(resign, signer=other), 1, lambda r: r.out == [f"{other.fingerprint}  -"],
            1, f"FAILED (signing-key.pem has fingerprint {other.fingerprint}, not the register's)",
        ),
        "a file added": (added, 3, lambda r: "> extra.txt" in r.out, 3, "FAILED (extra.txt is not listed)"),
        "a file removed": (
            lambda f: (f / "csv/graph.csv").unlink(), 3, lambda r: "< csv/graph.csv" in r.out,
            3, "FAILED (csv/graph.csv is listed but missing)",
        ),
        "a forged VERIFY.txt": (
            forged_verify, 0, lambda r: any("true" in line for line in r.out),
            0, "FAILED (VERIFY.txt from its line 3 is not deltasleuth-pack/1 verification)",
        ),
        "shell in line 2": (
            shell_in_line_2, 0, lambda r: r.out == ["line 2 of VERIFY.txt is malformed"],
            0, "FAILED (line 2 of VERIFY.txt is malformed)",
        ),
        "a line before the header": (
            line_before_the_header, 0, lambda r: "< echo injected" in r.out,
            0, "FAILED (VERIFY.txt from its line 3 is not deltasleuth-pack/1 verification)",
        ),
        "two key blocks, the first with a header": (
            two_key_blocks, 2, lambda r: r.out != ["Verified OK"],
            1, "FAILED (signing-key.pem is not one PEM EC P-256 public key)",
        ),
        "an edited audit text": (
            edited_text, 6, lambda r: r.out == [f"row {rows[grant][0]} altered"],
            6, f"FAILED (row {rows[grant][0]} altered)",
        ),
        "another cycle's attests.json": (
            other_cycle, 5, lambda r: r.out[0] == not_decided, 5, undecided,
        ),
        "step 1 of 2 granted": (half_granted, 5, lambda r: r.out[0] == not_decided, 5, undecided),
        "a revoke row": (revoke_row, 5, lambda r: r.out[0] == not_decided, 5, undecided),
        "the last chain row dropped": (
            last_dropped, 7, lambda r: any(last_hash in line for line in r.out), 7, _step_7(pristine),
        ),
    }  # fmt: skip
    for index, (name, (tamper, number, shows, checked, said)) in enumerate(cases.items()):
        folder = _copy(pristine, tmp_path / f"case-{index}")
        tamper(folder)
        for variant in VARIANTS:
            ran = run_with(folder, number, variant)
            assert shows(ran), (name, variant, ran)  # the procedure reads output, not exit codes
        found = verify(folder, pack_ctx)
        assert found[checked].startswith(said), (name, found)
        if name in ("the last chain row dropped", "step 1 of 2 granted", "a revoke row"):
            assert run_step(folder, 6).out == [], name  # consistent: step 6 cannot see it
        if name == "the last chain row dropped":  # only the SIEM (step 7) shows a cut end
            assert all(found[n].startswith("ok") for n in (0, 1, 2, 3, 5, 6)), found
        if name == "step 1 of 2 granted":
            assert any(line.startswith("< ") for line in run_step(folder, 7).out)  # not the SIEM's hashes
        if name in ("shell in line 2", "a line before the header"):  # read, never run
            assert all("injected" not in run_step(folder, n).out for n in range(8)), name
        if name == "two key blocks, the first with a header":
            assert found[2] == "FAILED", found


def _steps(path: Path, ctx: Context) -> dict[tuple[int, str], str]:
    """``ds packs verify``'s steps by number and name (a sampled pack's hand check follows step 5)."""
    zipped = path if path.suffix == ".zip" else rezip(path, path.parent)
    found = pack_verify.verify(zipped, ctx.signer.fingerprint)  # type: ignore[union-attr]
    return {(step.number, step.name): step.line.split(": ", 1)[1] for step in found}


async def test_a_sampled_pack_passes_its_procedure_and_hand_check_and_each_sample_tamper_fails(
    pack_ctx: Context, tmp_path: Path
) -> None:
    signed, issued, zipped, pristine = await _issued(pack_ctx, tmp_path, sampled=True)
    signer = pack_ctx.signer
    assert signer is not None
    drawn = json.loads((pristine / "facts/attests.json").read_text())["sample"]
    seq, selection, salt = drawn["draw_seq"], drawn["selection_sha256"], f"cutover-q4:{drawn['nonce']}"
    for variant in VARIANTS:  # VERIFY.txt unchanged, deltasleuth-pack/1: every step as for a census pack
        ran = {n: run_with(pristine, n, variant) for n in range(8)}
        assert (ran[0].code, ran[0].out) == (0, []), variant
        assert ran[1].out == [f"{signer.fingerprint}  -"] and ran[2].out == ["Verified OK"], variant
        assert ran[3].code == 0 and all(line.endswith(": OK") for line in ran[3].out), variant
        assert '    "basis": "sample",' in ran[4].out, variant  # step 4 prints it for the change record
        assert ran[5].out == [f"{signed.signoff_id}: decided", f"{signed.spec_approval_id}: decided", "1"]
        assert (ran[6].out, ran[7].out) == ([], [issued["sums_sha256"]]), variant
        assert all(r.err == "" for r in ran.values()), variant
        if variant != "libressl":  # the hand check needs no openssl
            text, digest = hand_check(pristine, seq, salt, variant)
            assert '"action": "sample.draw"' in text and f'"object": "{signed.run_id}"' in text
            assert f'"salt_sha256": "{digest}"' in text and f'"selection_sha256": "{selection}"' in text
    shown = _steps(zipped, pack_ctx)
    assert list(shown)[4:8] == [(4, "subject"), (5, "decisions"), (5, "sample draw"), (6, "audit rows")]
    assert shown[(4, "subject")].startswith(
        f"run {signed.run_id} cycle 1; basis sample; spec customer-profile"
    )
    assert shown[(5, "sample draw")] == (
        f"ok (row {seq} is draw {signed.draw_id}'s sample.draw row and holds both SHA-256 values; the SHA-256"
        f" of {salt} is the salt's)"
    )
    assert all(line.startswith("ok") for key, line in shown.items() if key[0] not in (4, 7, 8)), shown
    result = CliRunner().invoke(app, ["packs", "verify", str(zipped), "--fingerprint", signer.fingerprint])
    assert result.exit_code == 0 and f"step 5 sample draw: {shown[(5, 'sample draw')]}" in result.stdout

    rows = chain_rows(pristine)
    at = next(i for i, row in enumerate(rows) if row[0] == str(seq))
    forged = sampling.nonce()

    def facts_edited(folder: Path) -> None:  # a stratum's count, re-signed by the key's holder
        attests = json.loads((folder / "facts/attests.json").read_text())
        attests["sample"]["judgement"]["strata"][0]["n"] += 1
        (folder / "facts/attests.json").write_text(json.dumps(attests, sort_keys=True))
        resign(folder, signer)

    def row_altered(folder: Path, rehash: bool) -> None:  # another selection in the draw's row
        edited = chain_rows(folder)
        edited[at][5] = edited[at][5].replace(selection, "0" * 64)
        write_chain(
            folder, edited, rehash=rehash
        )  # rehash: rewritten consistently, as a database owner could
        resign(folder, signer)

    def unhashed_salt(folder: Path) -> None:  # another nonce, the sign-off's hash and the chain rewritten
        path = folder / "facts/attests.json"
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        attests = json.loads(path.read_text())
        attests["sample"]["nonce"] = forged
        path.write_text(json.dumps(attests, sort_keys=True))
        after = hashlib.sha256(path.read_bytes()).hexdigest()
        write_chain(folder, [[*row[:5], row[5].replace(before, after)] for row in chain_rows(folder)])
        resign(folder, signer)

    undecided = f"{signed.signoff_id}: NOT DECIDED"
    unheld = f"FAILED (row {seq} does not hold the selection SHA-256 of facts/attests.json)"
    cases: dict[str, tuple[Callable[[Path], None], int, Callable[[Ran], bool], tuple[int, str], str]] = {
        # name: (tamper, the step that catches it, how it shows there, ds packs verify's step, its text)
        "the sample facts edited": (
            facts_edited, 5, lambda r: r.out[0] == undecided,
            (5, "decisions"), f"FAILED (sign-off {signed.signoff_id} is NOT DECIDED)",
        ),
        "the draw row altered": (
            partial(row_altered, rehash=False), 6, lambda r: r.out == [f"row {seq} altered"],
            (5, "sample draw"), unheld,
        ),
        "the draw row rewritten consistently": (
            partial(row_altered, rehash=True), 7, lambda r: any(line.startswith("< ") for line in r.out),
            (5, "sample draw"), unheld,
        ),
        "a salt that does not hash": (
            unhashed_salt, 7, lambda r: any(line.startswith("< ") for line in r.out),
            (5, "sample draw"), f"FAILED (the SHA-256 of cutover-q4:{forged} is not the salt SHA-256 row"
            f" {seq} holds)",
        ),
    }  # fmt: skip
    for index, (name, (tamper, number, shows, checked, said)) in enumerate(cases.items()):
        folder = _copy(pristine, tmp_path / f"case-{index}")
        tamper(folder)
        for variant in VARIANTS:
            assert shows(run_with(folder, number, variant)), (name, variant)
        found = _steps(folder, pack_ctx)
        assert found[checked] == said, (name, found)
        if name == "the draw row altered":
            assert found[(6, "audit rows")] == f"FAILED (row {seq} altered)"
        if name in ("the draw row rewritten consistently", "a salt that does not hash"):
            assert run_step(folder, 6).out == [], name  # consistent: only the SIEM and the hand check see it
            assert all(found[(n, s)].startswith("ok") for n, s in found if n in (0, 1, 2, 3, 6)), found
        if name == "a salt that does not hash":
            assert run_step(folder, 5).out[:2] == [
                f"{signed.signoff_id}: decided",
                f"{signed.spec_approval_id}: decided",
            ]
            text, digest = hand_check(folder, seq, f"cutover-q4:{forged}", "sha256sum")
            assert f'"salt_sha256": "{digest}"' not in text


async def test_a_crafted_sample_fails_the_hand_check_and_never_crashes_the_verifier(
    pack_ctx: Context, tmp_path: Path
) -> None:
    """facts/attests.json nested past the parser's depth, or a salt label holding a lone surrogate, re-signed
    by the key's holder: ds packs verify prints every step, the hand check FAILED, and exits 1, never 3."""
    _, _, _, pristine = await _issued(pack_ctx, tmp_path, sampled=True)
    signer = pack_ctx.signer
    assert signer is not None
    drawn = json.loads((pristine / "facts/attests.json").read_text())["sample"]
    nested, surrogate = _copy(pristine, tmp_path / "nested"), _copy(pristine, tmp_path / "surrogate")
    (nested / "facts/attests.json").write_text("[" * 200_000 + "]" * 200_000)
    attests = json.loads((surrogate / "facts/attests.json").read_text())
    attests["sample"]["salt_label"] = "\ud800"
    (surrogate / "facts/attests.json").write_text(json.dumps(attests, sort_keys=True))  # as \\ud800
    salt, seq = f"\\ud800:{drawn['nonce']}", drawn["draw_seq"]  # as printed: escaped
    cases = {
        nested: "FAILED (facts/attests.json's sample cannot be read)",
        surrogate: f"FAILED (the SHA-256 of {salt} is not the salt SHA-256 row {seq} holds)",
    }
    for folder, said in cases.items():
        resign(folder, signer)
        zipped = folder.parent / "crafted.zip"
        with zipfile.ZipFile(zipped, "w") as zf:  # stored: the brackets deflate past the 100:1 limit
            for file in sorted(p for p in folder.rglob("*") if p.is_file()):
                zf.write(file, f"{folder.name}/{file.relative_to(folder)}")
        shown = _steps(zipped, pack_ctx)
        assert (shown[(5, "sample draw")], shown[(5, "decisions")][:6]) == (said, "FAILED"), folder
        verified = ["packs", "verify", str(zipped), "--fingerprint", signer.fingerprint]
        result = CliRunner().invoke(app, verified)
        assert result.exit_code == 1 and f"step 5 sample draw: {said}\n" in result.stdout, result.output
        assert result.stdout.count("\nstep ") == len(shown) - 1  # every step printed


async def test_a_labelled_pack_passes_its_procedure_and_each_tamper_of_its_labelling_fails(
    pack_ctx: Context, tmp_path: Path
) -> None:
    tools()
    signed, row = await labelled_cycle(pack_ctx)
    signer, labelling_id = pack_ctx.signer, row["labelling_id"]
    assert signer is not None
    issued = await pack.issue(pack_ctx, signed.run_id, 1, who=signed.people["olga"])
    _, data = await pack.stored(pack_ctx, issued["pack_id"])
    zipped = tmp_path / "pack.zip"
    zipped.write_bytes(data)
    pristine = unzip(data, tmp_path / "pristine")
    file_procedure(pristine, signer)
    await siem_export(pack_ctx, pristine)
    for variant in VARIANTS:  # VERIFY.txt unchanged, deltasleuth-pack/1: every step as for any pack
        ran = {n: run_with(pristine, n, variant) for n in range(8)}
        assert (ran[0].code, ran[0].out) == (0, []), variant
        assert ran[1].out == [f"{signer.fingerprint}  -"] and ran[2].out == ["Verified OK"], variant
        assert ran[3].code == 0 and "accuracy/labels.csv: OK" in ran[3].out, variant
        assert ran[5].out == [f"{signed.signoff_id}: decided", f"{signed.spec_approval_id}: decided", "1"]
        assert (ran[6].out, ran[7].out) == ([], [issued["sums_sha256"]]), variant
        assert all(r.err == "" for r in ran.values()), variant
    rows = chain_rows(pristine)
    at = next(i for i, r in enumerate(rows) if r[4] == labelling_id and '"action": "accuracy.label"' in r[5])
    seq, labels = rows[at][0], row["labels_sha256"]
    shown = _steps(zipped, pack_ctx)
    line = (5, "labelled accuracy")
    assert list(shown)[4:8] == [(4, "subject"), (5, "decisions"), line, (6, "audit rows")]
    assert shown[line] == (
        f"ok (row {seq} is labelling {labelling_id}'s accuracy.label row and holds the SHA-256 of"
        " accuracy/labels.csv and accuracy/score.json, as facts/attests.json does)"
    )
    assert all(text.startswith("ok") for key, text in shown.items() if key[0] not in (4, 7, 8)), shown
    result = CliRunner().invoke(app, ["packs", "verify", str(zipped), "--fingerprint", signer.fingerprint])
    assert result.exit_code == 0 and f"step 5 labelled accuracy: {shown[line]}" in result.stdout

    def score_edited(folder: Path, signed_again: bool) -> None:  # another count in the score
        path = folder / "accuracy/score.json"
        path.write_text(path.read_text().replace('"indeterminate": 0', '"indeterminate": 9'))
        if signed_again:
            resign(folder, signer)

    def labels_removed(folder: Path) -> None:
        (folder / "accuracy/labels.csv").unlink()
        resign(folder, signer)

    def row_dropped(folder: Path) -> None:  # the chain rewritten consistently without the label's row
        write_chain(folder, [r for i, r in enumerate(chain_rows(folder)) if i != at])
        resign(folder, signer)

    def row_altered(folder: Path) -> None:  # another labels hash in the row, the chain rewritten
        edited = chain_rows(folder)
        edited[at][5] = edited[at][5].replace(labels, "0" * 64)
        write_chain(folder, edited)
        resign(folder, signer)

    def attests(folder: Path, **members: object) -> None:  # the attested labelling edited, re-signed
        path = folder / "facts/attests.json"
        facts = json.loads(path.read_text())
        facts["accuracy"] |= members
        path.write_text(json.dumps(facts, sort_keys=True))
        resign(folder, signer)

    def nested(folder: Path) -> None:
        (folder / "facts/attests.json").write_text("[" * 200_000 + "]" * 200_000)
        resign(folder, signer)

    score = "accuracy/score.json is missing or does not hash to facts/attests.json's accuracy.score_sha256"
    unread = "FAILED (facts/attests.json's accuracy cannot be read)"
    cases: dict[str, tuple[Callable[[Path], None], str, bool]] = {
        # name: (tamper, the labelled line, whether step 3 still passes)
        "the score edited": (partial(score_edited, signed_again=False), f"FAILED ({score})", False),
        "the score edited and signed again": (
            partial(score_edited, signed_again=True), f"FAILED ({score})", True,
        ),
        "the labels removed": (
            labels_removed,
            "FAILED (accuracy/labels.csv is missing or does not hash to facts/attests.json's"
            " accuracy.labels_sha256)",
            True,
        ),
        "the label row dropped": (
            row_dropped,
            f"FAILED (audit/chain.tsv holds no accuracy.label row of labelling {labelling_id})",
            True,
        ),
        "the label row holding another hash": (
            row_altered,
            f"FAILED (row {seq} does not hold facts/attests.json's accuracy.labels_sha256 and score_sha256)",
            True,
        ),
        "a labelling id that is no text": (partial(attests, labelling_id=7), unread, True),
        "a hash that is no text": (partial(attests, score_sha256=None), unread, True),
        "attests nested past the parser": (nested, unread, True),
        "a lone surrogate for an id": (
            partial(attests, labelling_id="\ud800"),
            "FAILED (audit/chain.tsv holds no accuracy.label row of labelling \\ud800)",
            True,
        ),
    }  # fmt: skip
    for index, (name, (tamper, said, listed)) in enumerate(cases.items()):
        folder = _copy(pristine, tmp_path / f"case-{index}")
        tamper(folder)
        crafted = folder.parent / "crafted.zip"
        with zipfile.ZipFile(crafted, "w") as zf:  # stored: the brackets deflate past the 100:1 limit
            for file in sorted(path for path in folder.rglob("*") if path.is_file()):
                zf.write(file, f"{folder.name}/{file.relative_to(folder)}")
        found = _steps(crafted, pack_ctx)
        assert found[line] == said, (name, found)
        assert found[(3, "files")].startswith("ok") is listed, (name, found)
        if not listed:  # the procedure's own step 3 catches a file edited by anyone but the key's holder
            assert found[(3, "files")] == "FAILED (accuracy/score.json differs from its SHA256SUMS line)"
            assert "accuracy/score.json: FAILED" in run_step(folder, 3).out
        verified = ["packs", "verify", str(crafted), "--fingerprint", signer.fingerprint]
        result = CliRunner().invoke(app, verified)
        assert result.exit_code == 1 and f"step 5 labelled accuracy: {said}\n" in result.stdout, name
        assert result.stdout.count("\nstep ") == len(found) - 1, name  # every step printed, never a crash


async def test_a_revocation_list_refuses_a_revoked_pack_an_older_list_a_renamed_sums_and_a_cut_end(
    pack_ctx: Context, tmp_path: Path
) -> None:
    signer = pack_ctx.signer
    assert signer is not None
    signed, issued, zipped, folder = await _issued(pack_ctx, tmp_path)
    older = tmp_path / "older"  # the custodian's list of 2020, before the pack
    older.mkdir()
    header = pack.REVOCATIONS.format(fp=signer.fingerprint, at="2020-01-01T00:00:00Z")
    text = f"{header}\n# end\n".encode()
    for name, content in (("revoked.txt", text), ("revoked.txt.sig", signer.sign(text)),
                          ("signing-key.pem", signer.public_pem)):  # fmt: skip
        (older / name).write_bytes(content)
    renamed = tmp_path / "renamed"  # the pack's own signed SHA256SUMS passed off as a list
    renamed.mkdir()
    for name, source in (("revoked.txt", "SHA256SUMS"), ("revoked.txt.sig", "SHA256SUMS.sig"),
                         ("signing-key.pem", "signing-key.pem")):  # fmt: skip
        shutil.copy(folder / source, renamed / name)
    cut = await _revocations(pack_ctx, tmp_path / "cut")
    truncated = (cut / "revoked.txt").read_bytes().removesuffix(b"# end\n")
    (cut / "revoked.txt").write_bytes(truncated)
    (cut / "revoked.txt.sig").write_bytes(signer.sign(truncated))
    other = PackSigner(ec.generate_private_key(ec.SECP256R1()))
    doubled = tmp_path / "doubled"  # another key's list, its block before the register's
    doubled.mkdir()
    text = f"{pack.REVOCATIONS.format(fp=signer.fingerprint, at='2099-01-01T00:00:00Z')}\n# end\n".encode()
    for name, content in (("revoked.txt", text), ("revoked.txt.sig", other.sign(text)),
                          ("signing-key.pem", _two_blocks(other, signer))):  # fmt: skip
        (doubled / name).write_bytes(content)

    assert run_step(folder, 8, cwd=older).out[1:] == ["Verified OK", header, "# end", "0"]  # the date: judged
    assert verify(zipped, pack_ctx, older)[8] == (
        "FAILED (the list is as of 2020-01-01T00:00:00Z, before the pack was issued: get the latest)"
    )
    shown = run_step(folder, 8, cwd=renamed).out
    assert shown[1] == "Verified OK" and not shown[2].startswith("# DeltaSleuth pack revocations")
    assert (
        verify(zipped, pack_ctx, renamed)[8]
        == "FAILED (revoked.txt is not a revocation list of the register's key)"
    )
    assert run_step(folder, 8, cwd=cut).out[3] != "# end"
    assert (
        verify(zipped, pack_ctx, cut)[8] == "FAILED (revoked.txt does not end with '# end': it is incomplete)"
    )
    assert verify(zipped, pack_ctx, doubled)[8] == (
        "FAILED (the list's signing-key.pem is not one PEM EC P-256 public key)"
    )

    # The sign-off's revoke revokes the pack (ds.tg_signoff_packs), audited pack.revoke; the list names it.
    carol = signed.people["carol"]
    async with pack_ctx.pool.connection() as conn:
        await approvals.revoke(
            conn, partial(repo.insert_audit, conn), signed.signoff_id, "wrong filter", who=carol
        )
    async with pack_ctx.pool.connection() as conn:
        after = await repo.get_pack(conn, issued["pack_id"])
        cur = await conn.execute(
            "SELECT actor, detail FROM ds.audit_log WHERE action = 'pack.revoke' AND object = %s",
            (issued["pack_id"],),
        )
        audited = await cur.fetchall()
    assert after is not None
    assert (after["status"], after["revoke_reason"], after["revoked_by"]) == (
        "revoked",
        "wrong filter",
        carol.actor,
    )
    assert audited == [(carol.actor, {"reason": "wrong filter", "cascade_of": signed.signoff_id})]
    listed = await _revocations(pack_ctx, tmp_path / "listed")
    lines = (listed / "revoked.txt").read_text().splitlines()
    at = f"{after['revoked_at']:%Y-%m-%dT%H:%M:%SZ}"
    assert lines[1:] == [f"{issued['pack_id']} {issued['sums_sha256']} {at} {signer.fingerprint}", "# end"]
    assert run_step(folder, 8, cwd=listed).out[-1] == "1"
    assert verify(zipped, pack_ctx, listed)[8].startswith("FAILED (the pack is revoked (list as of ")


def _with(
    tmp_path: Path, files: dict[str, bytes], folder: str, name: str, extra: Callable[[zipfile.ZipFile], None]
) -> Path:
    """A zip of the pack's ``files`` and whatever ``extra`` adds."""
    path = tmp_path / f"{name}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for inner, content in files.items():
            zf.writestr(f"{folder}/{inner}", content)
        extra(zf)
    return path


@pytest.mark.filterwarnings("ignore:Duplicate name")  # zipfile writes the doubled entries, warning
async def test_ds_packs_verify_refuses_unsafe_zips_and_other_formats(
    pack_ctx: Context, tmp_path: Path
) -> None:
    _, issued, zipped, folder = await _issued(pack_ctx, tmp_path)
    fingerprint = pack_ctx.signer.fingerprint  # type: ignore[union-attr]
    with zipfile.ZipFile(zipped) as zf:
        files = {name.split("/", 1)[1]: zf.read(name) for name in zf.namelist()}
    name = issued["pack_id"]

    def symlink(zf: zipfile.ZipFile) -> None:
        info = zipfile.ZipInfo(f"{name}/link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "/etc/passwd")

    def many(zf: zipfile.ZipFile) -> None:
        for i in range(65 - len(files)):
            zf.writestr(f"{name}/extra/{i}.txt", "x")

    def large(zf: zipfile.ZipFile) -> None:
        info = zipfile.ZipInfo(f"{name}/large.bin")
        info.compress_type = zipfile.ZIP_STORED  # 1:1, so that only the size is over the limit
        with zf.open(info, "w", force_zip64=True) as out:
            for _ in range(200):  # with the pack's own files, 201 MiB
                out.write(bytes(1 << 20))

    def dense(zf: zipfile.ZipFile) -> None:
        zf.writestr(f"{name}/zeros.bin", bytes(1 << 20))  # deflates about 1,000:1

    def composed(zf: zipfile.ZipFile) -> None:
        zf.writestr(f"{name}/caf\u00e9.txt", "x")
        zf.writestr(f"{name}/cafe\u0301.txt", "x")  # the same name, decomposed

    outside = "'..': refused"
    refused: dict[str, tuple[Callable[[zipfile.ZipFile], None], str]] = {
        "slip": (lambda zf: zf.writestr(f"{name}/../evil.txt", "x"),
                 f"the zip's entry '{name}/../evil.txt' is an absolute path or holds {outside}"),
        "absolute": (lambda zf: zf.writestr("/etc/evil.txt", "x"),
                     f"the zip's entry '/etc/evil.txt' is an absolute path or holds {outside}"),
        "symlink": (symlink, f"the zip's entry '{name}/link' is a symlink: refused"),
        "65 entries": (many, "the zip holds 65 entries, over 64: refused"),
        "201 MiB": (large, "the zip would be 201 MiB uncompressed, over 200 MiB: refused"),
        "101:1": (dense, f"the zip's entry '{name}/zeros.bin' is compressed over 100:1: refused"),
        "another folder": (lambda zf: zf.writestr("other/x.txt", "x"),
                           "the zip does not hold one P-… folder and nothing else: refused"),
        # A second copy that an extractor keeping the first shows the auditor, unchecked.
        "a name twice": (lambda zf: zf.writestr(f"{name}/manifest.json", b"{}"),
                         f"the zip holds '{name}/manifest.json' twice: refused"),
        "a name twice, but for case": (lambda zf: zf.writestr(f"{name}/Certificate.pdf", b"%PDF"),
                                       f"the zip holds '{name}/Certificate.pdf' twice: refused"),
        "a name twice, but for NFC": (composed, f"the zip holds {name + '/cafe\u0301.txt'!r} twice: refused"),
    }  # fmt: skip
    for case, (extra, message) in refused.items():
        with pytest.raises(pack_verify.UnsafePack) as caught:
            pack_verify.verify(_with(tmp_path, files, name, case, extra), fingerprint)
        assert str(caught.value) == message, case
    manifest = json.loads(files["manifest.json"]) | {"format": "deltasleuth-pack/2"}
    relabelled = files | {"manifest.json": json.dumps(manifest).encode()}
    other = _with(tmp_path, relabelled, name, "format", lambda zf: None)
    result = CliRunner().invoke(app, ["packs", "verify", str(other), "--fingerprint", fingerprint])
    assert (result.exit_code, result.stderr) == (
        2, "error: the pack's format is 'deltasleuth-pack/2', not deltasleuth-pack/1: refused\n"
    )  # fmt: skip
    # A manifest the steps cannot read as they read it is refused, not a crash (exit 3).
    genuine = json.loads(files["manifest.json"])
    subject = genuine["subject"]
    for case, manifest in {
        "an array": [genuine["format"]],
        "a subject not an object": genuine | {"subject": "forged"},
        "a side not an object": genuine | {"subject": subject | {"sides": {"a": "x"}}},
        "a spec not an object": genuine | {"subject": subject | {"spec": ["x"]}},
        "an issued_at not a string": genuine | {"issued_at": 1},
        "an issued_at without its offset": genuine | {"issued_at": "2026-09-28T10:00:00"},
        "a pack_id not a string": genuine | {"pack_id": None},
    }.items():
        unreadable = _with(tmp_path, files | {"manifest.json": json.dumps(manifest).encode()}, name, case,
                           lambda zf: None)  # fmt: skip
        result = CliRunner().invoke(app, ["packs", "verify", str(unreadable), "--fingerprint", fingerprint])
        assert (result.exit_code, result.stderr) == (
            2, "error: the pack has no readable manifest.json: refused\n"
        ), case  # fmt: skip
    nested = b"[" * 100_000 + b"]" * 100_000  # stored, within every zip limit, too deep for json
    deep = _with(tmp_path, {k: v for k, v in files.items() if k != "manifest.json"}, name, "deep",
                 lambda zf: zf.writestr(f"{name}/manifest.json", nested, zipfile.ZIP_STORED))  # fmt: skip
    result = CliRunner().invoke(app, ["packs", "verify", str(deep), "--fingerprint", fingerprint])
    assert (result.exit_code, result.stderr) == (2, f"error: {pack_verify._UNREADABLE}\n")
    missing = CliRunner().invoke(app, ["packs", "verify", str(zipped)])
    assert (missing.exit_code, missing.stderr) == (
        2, "error: --fingerprint is required: the fingerprint your key register lists for the installation\n"
    )  # fmt: skip
    tampered = _copy(folder, tmp_path / "tampered")
    _flip(tampered / "facts/plan.json")
    zip_path = str(rezip(tampered, tampered.parent))
    failed = CliRunner().invoke(app, ["packs", "verify", zip_path, "--fingerprint", fingerprint])
    assert failed.exit_code == 1
    assert (
        "step 3 files: FAILED (facts/plan.json differs from its SHA256SUMS line)"
        in failed.stdout.splitlines()
    )
