"""Licence keys and entitlements: the file's grammar and signature, offline verification, the embedded and
removed keys, the standing judged at each date with its refusals, labels and warnings, stored rows that
grant nothing, pairs and their seats (``admit``, ``need``), and installing and withdrawing a licence."""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from deltasleuth import licence
from deltasleuth.config import Settings
from deltasleuth.context import Context
from deltasleuth.core.spec import TargetRegistry
from deltasleuth.licence import LicenceError, LicenceRefused, Standing
from deltasleuth.security import signing
from deltasleuth.security.signing import PackSigner
from deltasleuth.security.tokenise import Tokeniser
from deltasleuth.store import repo
from deltasleuth.store.db import migrate
from tests.conftest import TEST_KEYRING, TESTER, VENDOR_KEY, evidence_store, licence_file, licensed

pytestmark = pytest.mark.usefixtures("vendor_keys")  # a vendor build: test_licence_open.py holds the open one
INSTALLATION = "I-3f2a9c1b7d4e5a60"
OTHER = "I-aaaaaaaaaaaaaaaa"
LICENCE_ID = "LI-01JB8Q7ZK3M9W2XR5T4VNH6D0C"
LATER_ID = "LI-01JB8Q7ZK3M9W2XR5T4VNH6D0D"
STARTS, ENDS = date(2026, 10, 1), date(2027, 9, 30)
NOT_A_LICENCE = "the file is not a DeltaSleuth licence (deltasleuth-licence/1): "
FINGERPRINT = PackSigner(VENDOR_KEY).fingerprint
I1 = "this build embeds no vendor key: it needs no licence and can verify none"
M4 = (
    f"licence {LICENCE_ID} ended on 2027-09-30 and its 30 days of grace on 2027-10-30: new runs, compares,"
    " soak cycles and evidence packs are refused until a renewed licence is installed (ds licence install"
    " FILE); reads, reports, exports, holds, erasure, purge, audit, pack downloads and sign-offs under an"
    " Enterprise licence keep working"
)
M5 = (
    "SSO (auth.oidc) is an Enterprise feature and {whose}: under SSO this installation keeps serving sign-in,"
    " reads, reports, exports, pack downloads and administration, and new runs, compares and soak cycles"
    " need an Enterprise licence (ds licence install FILE){tail}"
)
TOKENS_TAIL, PACKS_TAIL = " or tokens mode", "; keep SSO, which evidence packs need"
LAST_START = " (this installation's server last started in oidc mode)"


def example(**fields: Any) -> bytes:
    """An example licence, signed by the session's vendor key; ``fields`` as ``licence_file``'s."""
    values = {"licence": LICENCE_ID, "issued": "2026-09-28T10:00:00Z", "starts": STARTS, "ends": ENDS}
    return licence_file(fields.pop("installation_id", INSTALLATION), **values | fields)


def stored(data: bytes, withdrawn_at: datetime | None = None) -> dict[str, Any]:
    """A ds.licences row of ``data`` as ``repo.licence_facts`` reads it."""
    text = licence.normalise(data)
    licence_id = re.search(r"^licence: (.*)$", text, re.MULTILINE)[1]  # type: ignore[index]
    return {"licence_id": licence_id, "text": text, "withdrawn_at": withdrawn_at}


def at(day: date, hour: int = 12, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), UTC)


def judged(
    *rows: dict[str, Any],
    day: date,
    mode: str = "none",
    start: dict[str, Any] | None = None,
    installs: tuple[dict[str, Any], ...] = (),
    newest: datetime | None = None,
    packs: bool = False,
    hour: int = 12,
    minute: int = 0,
) -> Standing:
    facts = {"installation_id": INSTALLATION, "now": None, "newest_run_at": newest, "start": start,
             "installs": list(installs), "rows": list(rows)}  # fmt: skip
    return licence.judge(facts, at(day, hour, minute), mode, packs=packs)


_PEOPLE = ("installed_by", "installed_by_name", "installed_at", "withdrawn_by", "withdrawn_by_name",
           "withdraw_reason")  # what a row of GET /v1/licence's installed adds  # fmt: skip


def pairs(*names: str) -> tuple[dict[str, Any], ...]:
    return tuple({"name": name, "first_at": at(STARTS)} for name in names)


def signed(body: str, key: ec.EllipticCurvePrivateKey) -> bytes:
    signature = base64.b64encode(PackSigner(key).sign(body.encode())).decode()
    return f"{body}signature: {signature}\n".encode()


# -- the file ---------------------------------------------------------------------------------------


def test_parse_example() -> None:
    data = example()
    found = licence.parse(data)
    assert [line.split(": ")[0] for line in found.text.splitlines()] == list(licence.FIELDS)
    assert found.text == data.decode() and found.body == data[: data.rindex(b"signature: ")]
    assert found.fields() == {
        "licence_id": LICENCE_ID, "sha256": hashlib.sha256(data).hexdigest(), "customer": "Example Bank plc",
        "order": "ORD-2026-014", "installation_id": INSTALLATION, "tier": "enterprise", "pairs": None,
        "starts": "2026-10-01", "ends": "2027-09-30", "issued": "2026-09-28T10:00:00Z", "key": FINGERPRINT,
    }  # fmt: skip
    assert licence.parse(example(tier="pro")).pairs == 5 and licence.new_id().startswith("LI-")
    assert re.fullmatch(r"LI-[0-9A-Z]{26}", licence.new_id())


def test_crlf_bom_blanks_verify() -> None:
    data = example()
    mangled = b"\xef\xbb\xbf\r\n" + b"".join(line + b" \t\r\n \n\n" for line in data.splitlines()) + b"\t"
    assert licence.normalise(mangled) == data.decode()
    assert licence.parse(mangled).text == data.decode()
    assert licence.parse(data.ljust(4096, b"\n")).text == data.decode()  # 4096 bytes are read


def _lines(change: Callable[[list[str]], list[str]]) -> bytes:
    return "".join(change(example().decode().splitlines(keepends=True))).encode()


@pytest.mark.parametrize(
    ("data", "why"),
    [
        (lambda: _lines(lambda lines: lines[:3] + lines[4:]), "line 4: expected 'order: …'"),
        (lambda: _lines(lambda lines: [lines[0], lines[1], lines[3], lines[2], *lines[4:]]),
         "line 3: expected 'customer: …'"),
        (lambda: _lines(lambda lines: [*lines, "note: extra\n"]), "line 13: expected the end of the file"),
        (lambda: _lines(lambda lines: ["format:deltasleuth-licence/1\n", *lines[1:]]),
         "line 1: expected 'format: deltasleuth-licence/1'"),
        (lambda: example(licence=LICENCE_ID.lower()), "licence is not LI- and 26 of 0-9 and A-Z"),
        (lambda: example(customer="Example\x85Bank"), "customer is not 1 to 200 printable characters"),
        (lambda: example(customer="E" * 201), "customer is not 1 to 200 printable characters"),
        (lambda: example(customer=" Example Bank plc"), "customer begins or ends with a blank"),
        (lambda: example(order="ORD-2026-014\u00a0"), "order begins or ends with a blank"),
        (lambda: example(tier="gold"), "tier is not community, pro or enterprise"),
        (lambda: example(pairs=0), "pairs is not unlimited or 1 to 9999"),
        (lambda: example(pairs="5.0"), "pairs is not unlimited or 1 to 9999"),
        (lambda: example(pairs=10000), "pairs is not unlimited or 1 to 9999"),
        (lambda: example(starts="2026-02-30"), "starts is not a date (YYYY-MM-DD)"),
        (lambda: example(ends=STARTS - timedelta(days=1)), "ends is before starts"),
        (lambda: example(ends=STARTS + timedelta(days=366)), "ends is more than 365 days after starts"),
        (lambda: example(issued="2026-09-28 10:00:00Z"), "issued is not a UTC time (YYYY-MM-DDTHH:MM:SSZ)"),
        (lambda: _lines(lambda lines: [*lines[:-1], "signature: not base64!\n"]), "signature is not base64"),
        (lambda: _lines(lambda lines: [*lines[:-1], lines[-1][:-1] + "\u00a0\n"]), "signature is not base64"),
        (lambda: example().ljust(4097, b"\n"), "over 4096 bytes"),
        (lambda: b"\xff" + example(), "not UTF-8 text"),
    ],
    ids=["missing", "moved", "extra", "format", "id", "control", "long", "leading", "trailing", "tier",
         "pairs-0", "pairs-5.0", "pairs-10000", "date", "ends-first", "366-days", "issued", "signature",
         "signature-nbsp", "4097-bytes", "utf-8"],
)  # fmt: skip
def test_grammar_I2(data: Callable[[], bytes], why: str) -> None:
    with pytest.raises(LicenceError) as refused:
        licence.parse(data())
    assert str(refused.value) == NOT_A_LICENCE + why


def test_no_key_I1(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(LicenceError, match=re.escape(I1)):
        licence.parse(example(), keys=())
    # A build before the maintainer's keys, by the keys read at each call; it holds after the bootstrap too,
    # when SHIPPED_KEYS is no longer empty and a release's checks run this test.
    monkeypatch.setattr(licence, "VENDOR_KEYS", ())
    with pytest.raises(LicenceError, match=re.escape(I1)):
        licence.parse(b"anything")


def test_newer_format_I3() -> None:
    newer = _lines(lambda lines: ["format: deltasleuth-licence/2\n", *lines[1:]])
    with pytest.raises(LicenceError) as refused:
        licence.parse(newer)
    assert str(refused.value) == (
        "licence format 'deltasleuth-licence/2' is newer than this build reads: upgrade DeltaSleuth"
    )


def test_untrusted_key_I4() -> None:
    other = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(LicenceError) as refused:
        licence.parse(example(key=other))
    assert str(refused.value) == (
        f"licence {LICENCE_ID} is signed by key {PackSigner(other).fingerprint[:12]}…, which this build does"
        " not trust: upgrade DeltaSleuth, or ask for a licence signed by a current key"
    )
    assert licence.parse(example(key=other), keys=[PackSigner(other).public_pem]).key != FINGERPRINT


def test_flipped_byte_and_borrowed_fingerprint_I5() -> None:
    flipped = example().replace(b"Bank plc", b"Bank plC")
    blank = example().decode().rsplit("signature: ", 1)[0].replace("Bank plc", "Bank plc ")
    trailing = signed(blank, VENDOR_KEY)  # a trailing blank signed, which no file as received keeps
    borrowed = signed(example().decode().rsplit("signature: ", 1)[0], ec.generate_private_key(ec.SECP256R1()))
    for data in (flipped, trailing, borrowed):
        with pytest.raises(LicenceError) as refused:
            licence.parse(data)
        assert str(refused.value) == (
            f"licence {LICENCE_ID} does not verify with its key {FINGERPRINT[:12]}…: the file was changed"
            " after it was signed; install the file as received"
        )


def _tools() -> None:
    """Skip without openssl and sha256sum; fail instead where they must be (DS_REQUIRE_TOOLS=1)."""
    missing = [tool for tool in ("openssl", "sha256sum", "awk", "sed") if shutil.which(tool) is None]
    if missing:
        (pytest.fail if os.environ.get("DS_REQUIRE_TOOLS") else pytest.skip)(f"missing {', '.join(missing)}")


# A licence checked by hand, with POSIX tools and openssl: the text as it is stored, its body and its
# signature, the key's fingerprint, and the verification.
RECIPE = r"""
LC_ALL=C awk '{ sub(/\r$/, ""); sub(/[ \t]+$/, ""); if (NR == 1) sub(/^\357\273\277/, "");
  if ($0 != "") print }' licence.txt > l.txt
sed '$d' l.txt > body.txt
sed -n 's/^signature: //p' l.txt | openssl base64 -d -A > sig.der
openssl pkey -pubin -in vendor.pem -outform DER | sha256sum
openssl dgst -sha256 -verify vendor.pem -signature sig.der body.txt
"""


def test_openssl_verifies(tmp_path: Path) -> None:
    """A licence verifies without the product: a file with a BOM, CRLFs, trailing blanks and empty lines."""
    _tools()
    data = example()
    (tmp_path / "licence.txt").write_bytes(b"\xef\xbb\xbf" + data.replace(b"\n", b" \t\r\n\r\n"))
    (tmp_path / "vendor.pem").write_bytes(PackSigner(VENDOR_KEY).public_pem)
    run = subprocess.run(["sh", "-c", RECIPE], cwd=tmp_path, capture_output=True, text=True, check=False)
    assert run.stdout.split() == [FINGERPRINT, "-", "Verified", "OK"], run.stderr
    assert (tmp_path / "l.txt").read_bytes() == data  # the recipe's normalised text is the stored one
    (tmp_path / "licence.txt").write_bytes(data.replace(b"Bank plc", b"Bank plC"))
    run = subprocess.run(["sh", "-c", RECIPE], cwd=tmp_path, capture_output=True, text=True, check=False)
    assert "Verification failure" in run.stdout + run.stderr


def test_shipped_keys_match_fixture(repo_root: Path) -> None:
    """The build embeds exactly the keys tests/fixtures/vendor-keys.txt lists as trusted, and the removed
    ones with their days."""
    listed = [
        line.split()
        for line in (repo_root / "tests/fixtures/vendor-keys.txt").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    for entry in listed:
        assert re.fullmatch(r"[0-9a-f]{64}", entry[0]), entry
        assert entry[1:] == ["trusted"] or (entry[1] == "removed" and date.fromisoformat(entry[2])), entry
    trusted = sorted(entry[0] for entry in listed if entry[1] == "trusted")
    assert sorted(signing.fingerprint(pem) for pem in licence.SHIPPED_KEYS) == trusted
    assert licence.REMOVED_KEYS == {entry[0]: entry[2] for entry in listed if entry[1] == "removed"}
    assert licence.VENDOR_KEYS == (PackSigner(VENDOR_KEY).public_pem,)  # the test's key, in tests only


# -- the standing -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "hour", "state"),
    [
        (ENDS - timedelta(days=31), 23, "valid"),
        (ENDS - timedelta(days=30), 0, "ending"),
        (ENDS, 23, "ending"),
        (ENDS + timedelta(days=1), 0, "grace"),
        (ENDS + timedelta(days=30), 23, "grace"),
        (ENDS + timedelta(days=31), 0, "expired"),
    ],
)
def test_judge_edges(day: date, hour: int, state: str) -> None:
    current = judged(stored(example()), day=day, hour=hour)
    assert (current.state, current.licence.licence_id, current.since) == (state, LICENCE_ID, at(STARTS, 0))  # type: ignore[union-attr]
    assert current.limit is None and current.refusal("run") == (M4 if state == "expired" else None)


def test_a_future_licence_alone_is_an_evaluation() -> None:
    current = judged(stored(example()), day=STARTS - timedelta(days=1), hour=23, minute=59)
    assert (current.state, current.licence, current.limit, current.since) == ("evaluation", None, 2, None)
    assert current.warnings()[1:] == [f"licence {LICENCE_ID} applies from 2026-10-01"]
    assert judged(stored(example()), day=STARTS, hour=0).state == "valid"


def test_what_a_server_start_records() -> None:
    """``system.start``'s ``licence``: ``pairs`` is the evaluation's two, a licence's limit, or null
    when unlimited."""
    none = {"installation_id": INSTALLATION, "licence_id": None, "tier": None, "ends": None}
    found = none | {"licence_id": LICENCE_ID, "state": "valid", "ends": "2027-09-30"}
    assert judged(day=STARTS).detail() == none | {"state": "evaluation", "pairs": 2}
    assert judged(stored(example(tier="pro")), day=STARTS).detail() == found | {"tier": "pro", "pairs": 5}
    assert judged(stored(example()), day=STARTS).detail() == found | {"tier": "enterprise", "pairs": None}


def test_renewal_silences_ending() -> None:
    renewal = example(licence=LATER_ID, starts=ENDS + timedelta(days=1), ends=date(2028, 9, 29))
    lesser = example(licence=LATER_ID, starts=ENDS + timedelta(days=1), ends=date(2028, 9, 29), tier="pro")
    day = ENDS - timedelta(days=12)
    alike = judged(stored(example()), stored(renewal), day=day)
    assert (alike.state, alike.renewal, alike.pending) == ("valid", licence.parse(renewal), ())
    assert alike.warnings() == []
    less = judged(stored(example()), stored(lesser), day=day)
    assert less.state == "ending" and less.warnings() == [
        f"licence {LICENCE_ID} ends on 2027-09-30 (in 12 days): install its renewal (ds licence install"
        f" FILE); new runs and evidence packs stop on 2027-10-31; {LATER_ID} (pro, 5 pairs) follows from"
        " 2027-10-01"
    ]
    after = judged(stored(example()), stored(lesser), day=ENDS + timedelta(days=1))
    assert (after.state, after.licence.licence_id, after.limit) == ("valid", LATER_ID, 5)  # type: ignore[union-attr]


def test_later_starts_wins_ties_by_issued() -> None:
    downgrade = example(licence=LATER_ID, starts=date(2027, 1, 1), ends=date(2027, 12, 31), tier="pro",
                        issued="2026-11-02T09:00:00Z")  # fmt: skip
    correction = example(licence="LI-" + "Z" * 26, starts=date(2027, 1, 1), ends=date(2027, 12, 31),
                         tier="community", issued="2026-12-20T09:00:00Z")  # fmt: skip
    before = judged(stored(example()), stored(downgrade), day=date(2026, 12, 31))
    assert before.licence.licence_id == LICENCE_ID  # type: ignore[union-attr]
    assert before.warnings() == [f"licence {LATER_ID} applies from 2027-01-01"]
    assert judged(stored(example()), stored(downgrade), day=date(2027, 1, 1)).licence.licence_id == LATER_ID  # type: ignore[union-attr]
    tied = judged(stored(example()), stored(correction), stored(downgrade), day=date(2027, 2, 1))
    assert (tied.licence.licence_id, tied.licence.tier) == ("LI-" + "Z" * 26, "community")  # type: ignore[union-attr]
    ended = judged(stored(example()), stored(downgrade), day=date(2028, 3, 1))
    assert (ended.state, ended.licence.licence_id) == ("expired", LATER_ID)  # type: ignore[union-attr]


def test_withdrawal_counts_only_before_starts() -> None:
    day = STARTS + timedelta(days=10)
    before = judged(stored(example(), withdrawn_at=at(STARTS - timedelta(days=1), 23, 59)), day=day)
    assert (before.state, before.licence) == ("evaluation", None)
    after = judged(stored(example(), withdrawn_at=at(STARTS, 0)), day=day)  # only SQL can write it
    assert after.state == "valid"


def test_foreign_or_edited_rows_grant_nothing() -> None:
    stranger = PackSigner(ec.generate_private_key(ec.SECP256R1()))
    foreign = stored(example(installation_id=OTHER))
    edited = stored(example()) | {"text": licence.normalise(example().replace(b"enterprise", b"enterprisf"))}
    untrusted = stored(example(licence=LATER_ID, key=stranger._key))
    current = judged(foreign, edited, untrusted, day=STARTS + timedelta(days=10))
    assert (current.state, current.licence) == ("evaluation", None)
    assert current.warnings()[1:] == [
        f"licence {LICENCE_ID} in the store does not count: it is for installation {OTHER}, not this one"
        f" ({INSTALLATION})",
        f"licence {LICENCE_ID} in the store does not count: its text is not a DeltaSleuth licence"
        " (deltasleuth-licence/1): tier is not community, pro or enterprise",
        f"licence {LATER_ID} in the store does not count: licence {LATER_ID} is signed by key"
        f" {stranger.fingerprint[:12]}…, which this build does not trust: upgrade DeltaSleuth, or ask for a"
        " licence signed by a current key",
    ]


def test_unreadable_rows_grant_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A row written with SQL whose signature line ends in a no-break space, or whose check raises, grants
    nothing beside the Enterprise licence and makes nothing raise."""
    nbsp = stored(example(licence=LATER_ID))
    nbsp["text"] = nbsp["text"][:-1] + "\u00a0\n"
    current = judged(stored(example()), nbsp, day=STARTS)
    assert (current.state, current.licence.licence_id) == ("valid", LICENCE_ID)  # type: ignore[union-attr]
    assert current.uncounted == ((LATER_ID, "its text is not a DeltaSleuth licence (deltasleuth-licence/1):"
                                            " signature is not base64"),)  # fmt: skip
    assert current.refusal("signoff") is None and current.refusal("packs") is None

    def broken(*_: Any) -> bool:
        raise RuntimeError("a bug")

    monkeypatch.setattr(signing, "verifies", broken)
    unread = "LI-" + "Y" * 26  # a text no other test judges: parsed afresh
    assert judged(stored(example(licence=unread)), day=STARTS).uncounted == (
        (unread, "its text cannot be read (RuntimeError)"),
    )


def test_rows_of_a_removed_key_are_not_warned(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a compromise release a row signed by the removed key grants nothing and is not warned of,
    its reissue in force; one signed by a key this build never knew still is."""
    removed, stranger = (PackSigner(ec.generate_private_key(ec.SECP256R1())) for _ in range(2))
    monkeypatch.setattr(licence, "REMOVED_KEYS", {removed.fingerprint: "2027-01-05"})
    old = stored(example(key=removed._key))
    reissue = stored(example(licence=LATER_ID, issued="2027-01-06T09:00:00Z"))
    unknown = stored(example(licence="LI-" + "U" * 26, key=stranger._key))
    current = judged(old, reissue, unknown, day=date(2027, 2, 1))
    why = (
        f"signed by key {removed.fingerprint[:12]}…, withdrawn on 2027-01-05 after a compromise; its reissue"
        " replaces it"
    )
    assert (current.state, current.retired) == ("valid", ((LICENCE_ID, why),))
    assert current.licence is not None and current.licence.licence_id == LATER_ID
    assert current.warnings() == [
        f"licence LI-{'U' * 26} in the store does not count: licence LI-{'U' * 26} is signed by key"
        f" {stranger.fingerprint[:12]}…, which this build does not trust: upgrade DeltaSleuth, or ask for a"
        " licence signed by a current key"
    ]
    assert licence._installed(current, old | {name: None for name in _PEOPLE})["problem"] == why
    past = judged(old, day=ENDS + timedelta(days=31))  # not reissued: its grace is over
    assert (past.state, past.warnings()[1:], past.refusal("signoff") is not None) == ("evaluation", [], True)


def test_missing_installed_row_warned() -> None:
    current = judged(stored(example()), day=STARTS, installs=({"id": 7, "object": LICENCE_ID},
                                                             {"id": 812, "object": LATER_ID}))  # fmt: skip
    assert current.state == "valid" and current.warnings() == [
        f"licence {LATER_ID} was installed (audit row 812) but is missing from the store: install it again"
        " (ds licence install FILE)"
    ]


def test_sso_fence_by_last_start() -> None:
    pro, enterprise = stored(example(tier="pro")), stored(example())
    oidc, tokens = {"auth": "oidc", "pack_key": None}, {"auth": "tokens", "pack_key": None}
    whose = f"licence {LICENCE_ID} is pro"
    assert judged(pro, day=STARTS, mode="oidc").refusal("run") == M5.format(whose=whose, tail=TOKENS_TAIL)
    last = judged(pro, day=STARTS, start=oidc)  # a CLI --config without auth.oidc
    assert last.refusal("run") == M5.format(whose=whose, tail=TOKENS_TAIL) + LAST_START
    keyed = judged(pro, day=STARTS, start=oidc | {"pack_key": "ab" * 32})
    assert keyed.refusal("run") == M5.format(whose=whose, tail=PACKS_TAIL) + LAST_START
    assert judged(pro, day=STARTS, mode="oidc", packs=True).refusal("run") == M5.format(
        whose=whose, tail=PACKS_TAIL
    )
    assert judged(pro, day=STARTS, start=tokens).refusal("run") is None
    assert judged(pro, day=STARTS + timedelta(days=400), mode="oidc").refusal("run") == M4
    assert judged(enterprise, day=STARTS, mode="oidc", start=oidc).refusal("run") is None
    graced = judged(pro, day=ENDS + timedelta(days=2), mode="oidc")
    assert (graced.state, graced.refusal("run")) == ("grace", M5.format(whose=whose, tail=TOKENS_TAIL))


def test_evaluation_fences_like_community() -> None:
    evaluation, community = judged(day=STARTS), judged(stored(example(tier="community")), day=STARTS)
    assert (evaluation.limit, community.limit) == (2, 2)
    assert [evaluation.refusal(feature) for feature in ("run", "signoff", "packs")] == [
        None,
        "four-eyes sign-off is an Enterprise feature and this installation is an unlicensed evaluation:"
        f" install an Enterprise licence for installation {INSTALLATION}",
        f"no evidence pack is issued in an unlicensed evaluation: install an Enterprise licence for"
        f" installation {INSTALLATION}",
    ]
    assert [community.refusal(feature) for feature in ("run", "signoff", "packs")] == [
        None,
        f"four-eyes sign-off is an Enterprise feature and licence {LICENCE_ID} is community",
        f"evidence packs are an Enterprise feature and licence {LICENCE_ID} is community",
    ]
    whose = "this installation is an unlicensed evaluation"
    assert judged(day=STARTS, mode="oidc").refusal("run") == M5.format(whose=whose, tail=TOKENS_TAIL)
    expired = judged(stored(example()), day=ENDS + timedelta(days=31), mode="oidc")  # Enterprise, ended
    assert [expired.refusal(feature) for feature in ("run", "signoff", "packs")] == [M4, None, M4]
    assert judged(stored(example()), day=STARTS, mode="oidc").refusal("packs") is None
    with pytest.raises(ValueError, match="unknown licensed feature 'reveal'"):
        evaluation.refusal("reveal")


def test_clock_behind_refuses_nothing() -> None:
    now = at(STARTS + timedelta(days=5))
    behind = judged(stored(example()), day=now.date(), newest=now + timedelta(days=3, hours=8))
    assert behind.clock_behind == timedelta(days=3, hours=8) and behind.refusal("run") is None
    assert behind.record() == {"licence_id": LICENCE_ID, "tier": "enterprise", "state": "valid",
                               "ends": "2027-09-30", "clock_behind": True}  # fmt: skip
    assert behind.warnings() == [
        "the database clock is 3 day(s) behind the newest run (2026-10-09T20:00Z): licence dates are judged"
        " by a clock that went back"
    ]
    assert judged(stored(example()), day=now.date(), newest=now + timedelta(minutes=5)).clock_behind is None


def test_labels_warnings_golden() -> None:
    run = {"run_id": "R-" + "A" * 26, "params": {}}
    evaluation = judged(day=STARTS).record()
    assert evaluation == {"licence_id": None, "tier": None, "state": "evaluation", "ends": None}
    grace = judged(stored(example()), day=ENDS + timedelta(days=3)).record()
    valid = judged(stored(example()), day=STARTS).record()
    records = (evaluation, grace, valid, evaluation | {"clock_behind": True})
    labels = [licence.run_label(run | {"params": {"licence": record}}) for record in records]
    assert labels == [
        f"Unlicensed evaluation: run {run['run_id']} was made without a licence. Not for production use; no"
        " evidence pack is issued for it.",
        f"Run {run['run_id']} was made in the 30-day grace of licence {LICENCE_ID}, which ended on"
        " 2027-09-30.",
        f"Run {run['run_id']} was made under licence {LICENCE_ID} (enterprise).",
        f"Unlicensed evaluation: run {run['run_id']} was made without a licence. Not for production use; no"
        " evidence pack is issued for it. The database clock was behind the newest run when it was made.",
    ]
    assert (
        licence.run_label(run) is None
        and licence.file_name(run, "report.pdf") == f"{run['run_id']}-report.pdf"
    )
    assert licence.file_name(run | {"params": {"licence": evaluation}}, "report.pdf") == (
        f"{run['run_id']}-unlicensed-evaluation-report.pdf"
    )
    assert replace(judged(day=STARTS), counted=pairs("customer-profile")).warnings() == [
        f"Unlicensed evaluation of installation {INSTALLATION}: at most 2 pairs (1 used), no runs under SSO,"
        " no sign-off or evidence packs; not licensed for production use (ds licence)"
    ]
    assert judged(stored(example()), day=ENDS - timedelta(days=1)).warnings() == [
        f"licence {LICENCE_ID} ends on 2027-09-30 (in 1 day): install its renewal (ds licence install FILE);"
        " new runs and evidence packs stop on 2027-10-31"
    ]
    assert (
        judged(stored(example()), day=ENDS)
        .warnings()[0]
        .startswith(f"licence {LICENCE_ID} ends on 2027-09-30 (today)")
    )
    assert judged(stored(example()), day=ENDS + timedelta(days=1)).warnings() == [
        f"licence {LICENCE_ID} ended on 2027-09-30: everything works until 2027-10-30, then new runs and"
        " evidence packs stop; install its renewal (ds licence install FILE)"
    ]
    assert judged(stored(example()), day=ENDS + timedelta(days=31)).warnings() == [M4]
    pro = judged(stored(example(tier="pro")), day=STARTS)
    five = ("accounts", "customers", "loans", "payments", "transfers")
    assert replace(pro, counted=pairs(*five)).warnings() == [
        f"licence {LICENCE_ID} allows 5 pairs and 5 have runs since 2026-10-01: a new pair is refused"
    ]
    assert replace(pro, counted=pairs(*five, "cards", "loans2")).warnings() == [
        f"licence {LICENCE_ID} allows 5 pairs and 7 have runs since 2026-10-01: cards and loans2 get no new"
        " run or cycle"
    ]
    assert replace(pro, counted=pairs(*five)).full("cards") == (
        f"spec cards would be pair 6 of 5 under licence {LICENCE_ID} (pro, 2026-10-01 to 2027-09-30): its"
        " seats since 2026-10-01 are accounts, customers, loans, payments, transfers, whose runs go ahead; a"
        " new pair needs a licence with more pairs"
    )
    assert replace(pro, counted=pairs(*five, "cards")).unseated("cards") == (
        f"spec cards is not one of the 5 pairs licence {LICENCE_ID} (pro, 2026-10-01 to 2027-09-30) seats"
        " since 2026-10-01 (accounts, customers, loans, payments, transfers): its cycles under way end, and a"
        " new run or cycle needs a licence with more pairs"
    )
    two = replace(judged(day=STARTS), counted=pairs("customer-profile", "linker", "cards"))
    assert two.full("cards") == (
        "spec cards would be a third pair: an unlicensed evaluation runs 2 pairs (customer-profile, linker);"
        f" install a licence for installation {INSTALLATION}"
    )
    assert two.unseated("cards") == (
        "spec cards is not one of the 2 pairs an unlicensed evaluation runs (customer-profile, linker): its"
        f" cycles under way end, and a new run or cycle needs a licence for installation {INSTALLATION}"
    )
    assert two.unseated("linker") is None and replace(pro, counted=pairs(*five)).unseated("loans") is None
    many = replace(pro, counted=pairs(*(f"spec-{n:02}" for n in range(1, 17))))
    assert many.warnings()[0].endswith(
        ": spec-06, spec-07, spec-08, spec-09, spec-10, spec-11, spec-12, spec-13, spec-14, spec-15, … and 1"
        " more get no new run or cycle"
    )


# -- in the store: install, withdraw, admit -----------------------------------------------------


@pytest.fixture
async def ctx(pg_dsn: str, tmp_path: Path) -> AsyncIterator[Context]:
    pool = AsyncConnectionPool(pg_dsn, open=False, max_size=4)
    await pool.open(wait=True)
    await migrate(pool)
    settings = Settings("file:/unused", tmp_path / "evidence", tmp_path / "ws", tmp_path / "targets.yaml")
    evidence = evidence_store(settings.evidence_dir)
    yield Context(
        settings, pool, TargetRegistry({}), Tokeniser(b"k" * 32), evidence, None, None, TEST_KEYRING
    )
    await pool.close()


async def _facts(ctx: Context) -> dict[str, Any]:
    async with ctx.pool.connection() as conn:
        return await repo.licence_facts(conn)


async def _today(ctx: Context) -> date:
    return (await _facts(ctx))["now"].astimezone(UTC).date()


async def _written(ctx: Context) -> tuple[list[tuple], list[tuple]]:
    """The licence rows and the licence audit rows."""
    async with ctx.pool.connection() as conn:
        rows = await (
            await conn.execute("SELECT licence_id, withdrawn_at FROM ds.licences ORDER BY 1")
        ).fetchall()
        audit = await (
            await conn.execute(
                "SELECT action, object, detail FROM ds.audit_log WHERE action LIKE 'licence.%' ORDER BY id"
            )
        ).fetchall()
    return rows, audit


async def ran(
    ctx: Context, name: str, *, created_at: datetime | None = None, finished_at: datetime | None = None
) -> str:
    """A run of a new version of spec ``name`` (targets ``<name>-a`` and ``<name>-b``), stored with SQL."""
    body = {"sides": {"a": {"target": f"{name}-a"}, "b": {"target": f"{name}-b"}}}
    run_id = repo.new_run_id()
    async with ctx.pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO ds.specs (name, version, content_hash, body, created_by)"
            " VALUES (%s, '1', %s, %s, %s) RETURNING spec_id",
            (name, uuid.uuid4().hex, Jsonb(body), TESTER.actor),
        )
        (spec_id,) = await cur.fetchone()  # type: ignore[misc]
        await conn.execute(
            "INSERT INTO ds.runs (run_id, spec_id, mode, status, created_by, created_at, finished_at)"
            " VALUES (%s, %s, 'batch', %s, %s, coalesce(%s, now()), %s)",
            (run_id, spec_id, "queued" if finished_at is None else "completed", TESTER.actor, created_at,
             finished_at),
        )  # fmt: skip
    return run_id


async def _admit(ctx: Context, name: str, *, lock: bool = False) -> tuple[dict[str, Any], int | None]:
    """``admit``'s record and, for a new pair, its count."""
    async with ctx.pool.connection() as conn:
        record, pair = await licence.admit(ctx, conn, SimpleNamespace(name=name), lock=lock)  # type: ignore[arg-type]
    return record, pair and pair[1]["count"]


@pytest.mark.db
async def test_install_stores_audits_and_applies(ctx: Context) -> None:
    facts = await _facts(ctx)
    installation, today = facts["installation_id"], facts["now"].astimezone(UTC).date()
    assert re.fullmatch(r"I-[0-9a-f]{16}", installation)
    async with ctx.pool.connection() as conn:
        assert (await licence.standing(ctx, conn)).state == "evaluation"
    data = licence_file(installation, starts=today)
    installed = await licence.install(ctx, data, who=TESTER)
    found = installed["licence"]
    assert (installed["installed"], installed["applies"], installed["before"].state) == (
        True,
        "now",
        "evaluation",
    )
    assert (installed["after"].state, installed["after"].licence) == ("valid", found)
    later = await licence.install(
        ctx, licence_file(installation, starts=today + timedelta(days=364)), who=TESTER
    )
    assert later["applies"] == f"from {today + timedelta(days=364)}"
    replaced = await licensed(ctx, starts=today, tier="pro", issued="2020-01-01T00:00:00Z")  # issued earlier
    again = await licence.install(ctx, data, who=TESTER)  # a restore may repeat it
    assert (again["installed"], again["applies"]) == (False, None)
    rows, audit = await _written(ctx)
    assert [row[0] for row in rows] == sorted(
        [found.licence_id, later["licence"].licence_id, replaced.licence_id]
    )
    assert audit[0] == ("licence.install", found.licence_id, {
        "sha256": found.sha256, "customer": "Example Bank plc", "order": "ORD-2026-014",
        "installation_id": installation, "tier": "enterprise", "pairs": None, "starts": str(today),
        "ends": str(today + timedelta(days=364)), "issued": f"{found.issued:%Y-%m-%dT%H:%M:%SZ}",
        "key": FINGERPRINT, "applies": "now", "asserted": True, "os_account": "tester",
    })  # fmt: skip
    assert [row[2]["applies"] for row in audit] == ["now", f"from {today + timedelta(days=364)}",
                                                    f"no: {found.licence_id} applies"]  # fmt: skip


@pytest.mark.db
async def test_install_refusals_write_nothing(ctx: Context, monkeypatch: pytest.MonkeyPatch) -> None:
    installation, today = (await _facts(ctx))["installation_id"], await _today(ctx)
    future = await licensed(ctx, starts=today + timedelta(days=10))
    await licence.withdraw(ctx, future.licence_id, "the wrong customer", who=TESTER)
    installed = await licensed(ctx)
    before = await _written(ctx)
    ended, grace = today - timedelta(days=31), today - timedelta(days=1)
    refusals = [
        (licence_file(OTHER),
         f"licence {{id}} is for installation {OTHER}, not this one ({installation}): ask for a licence for"
         f" {installation} (ds licence id)"),
        (licence_file(installation, starts=today - timedelta(days=100), ends=ended),
         f"licence {{id}} ended on {ended} and its grace on {grace}: install a current licence"),
        (licence_file(installation, licence=installed.licence_id, customer="Another Bank"),
         "licence {id} is installed already with other content: ask your DeltaSleuth contact which file is"
         " genuine"),
        (future.text.encode(),
         f"licence {{id}} was withdrawn on {today}: it is not installed again; ask for a new licence"),
        (b"format: deltasleuth-licence/1\n", NOT_A_LICENCE + "line 2: expected 'licence: …'"),
    ]  # fmt: skip
    for data, message in refusals:
        with pytest.raises(LicenceError) as refused:
            await licence.install(ctx, data, who=TESTER)
        found = re.search(rb"^licence: (.*)$", data, re.MULTILINE)
        assert str(refused.value) == message.format(id=found and found[1].decode())
    monkeypatch.setattr(licence, "VENDOR_KEYS", ())
    with pytest.raises(LicenceError, match=re.escape(I1)):
        await licence.install(ctx, installed.text.encode(), who=TESTER)
    assert await _written(ctx) == before


@pytest.mark.db
async def test_withdraw_only_before_starts(ctx: Context) -> None:
    today = await _today(ctx)
    started, future = await licensed(ctx, tier="pro"), await licensed(ctx, starts=today + timedelta(days=1))
    row = await licence.withdraw(ctx, future.licence_id, "issued for the wrong order", who=TESTER)
    assert (row["withdrawn_by"], row["withdraw_reason"]) == (TESTER.actor, "issued for the wrong order")
    refused = {
        started.licence_id: f"licence {started.licence_id} started on {today}: a started licence is replaced"
        " by a new one, not withdrawn",
        future.licence_id: f"licence {future.licence_id} is withdrawn already",
        LATER_ID: f"licence {LATER_ID} is not installed",
    }
    for licence_id, message in refused.items():
        with pytest.raises(LicenceError) as caught:
            await licence.withdraw(ctx, licence_id, "why", who=TESTER)
        assert str(caught.value) == message
    for reason in ("", "x" * 201, "line\nbreak"):
        with pytest.raises(
            LicenceError, match="--reason must be 1 to 200 characters without control characters"
        ):
            await licence.withdraw(ctx, started.licence_id, reason, who=TESTER)
    rows, audit = await _written(ctx)
    assert [(licence_id, withdrawn is None) for licence_id, withdrawn in rows] == [
        (started.licence_id, True),
        (future.licence_id, False),
    ]
    detail = {"reason": "issued for the wrong order", "asserted": True, "os_account": "tester"}
    assert audit[-1] == ("licence.withdraw", future.licence_id, detail)
    async with ctx.pool.connection() as conn:
        assert (await licence.standing(ctx, conn)).licence == started  # no withdrawal returns to evaluation


@pytest.mark.db
async def test_admit_seats_counts_and_refuses(ctx: Context) -> None:
    record = {"licence_id": None, "tier": None, "state": "evaluation", "ends": None}
    async with ctx.pool.connection() as conn:
        pair = (await licence.admit(ctx, conn, SimpleNamespace(name="customer-profile")))[1]  # type: ignore[arg-type]
    installation = (await _facts(ctx))["installation_id"]
    detail = {"since": None, "name": "customer-profile", "count": 1, "limit": 2, "targets": []}
    assert pair == (installation, detail)  # the licence.pair row, but its run_id
    assert await _admit(ctx, "customer-profile") == (record, 1)
    await ran(ctx, "customer-profile")
    assert await _admit(ctx, "customer-profile") == (record, None)  # seated: a new version or run is free
    assert await _admit(ctx, "linker", lock=True) == (record, 2)
    await ran(ctx, "linker")
    with pytest.raises(
        LicenceRefused, match=r"^spec cards would be a third pair: an unlicensed evaluation runs 2"
    ):
        await _admit(ctx, "cards", lock=True)
    await ran(ctx, "cards")  # as a release before 010 could
    with pytest.raises(
        LicenceRefused, match=r"^spec cards is not one of the 2 pairs an unlicensed evaluation"
    ):
        await _admit(ctx, "cards")
    async with ctx.pool.connection() as conn:
        with pytest.raises(
            LicenceRefused, match=r"^run R-1 cannot resume: spec cards is not one of the 2 pairs"
        ):
            await licence.need(ctx, conn, "run", name="cards", prefix="run R-1 cannot resume: ")
        assert (await licence.need(ctx, conn, "run", name="linker")).names == [
            "customer-profile",
            "linker",
            "cards",
        ]
        with pytest.raises(LicenceRefused, match=r"^four-eyes sign-off is an Enterprise feature"):
            await licence.need(ctx, conn, "signoff")
    await licensed(ctx, tier="enterprise")  # unlimited: nothing is counted
    assert (await _admit(ctx, "cards"))[1] is None and (await _admit(ctx, "loans", lock=True))[1] is None


@pytest.mark.db
async def test_a_licence_counts_from_its_starts(ctx: Context) -> None:
    today = await _today(ctx)
    since = datetime.combine(today, time(), UTC)
    await ran(ctx, "old", created_at=since - timedelta(days=9), finished_at=since - timedelta(seconds=1))
    await ran(ctx, "live", created_at=since - timedelta(days=5))  # live at since: seated first
    await ran(ctx, "new")
    found = await licensed(ctx, tier="pro", pairs=2)
    async with ctx.pool.connection() as conn:
        current = await licence.standing(ctx, conn)
        targets = await repo.pair_targets(conn, current.names)
    assert (current.since, current.names, current.seats) == (since, ["live", "new"], ["live", "new"])
    assert targets == {"live": ["live-a vs live-b"], "new": ["new-a vs new-b"]}
    assert await _admit(ctx, "live") == (current.record(), None)
    with pytest.raises(LicenceRefused) as refused:
        await _admit(ctx, "old")
    assert str(refused.value).startswith(
        f"spec old would be pair 3 of 2 under licence {found.licence_id} (pro,"
    )


@pytest.mark.db
async def test_a_new_pair_under_lock_holds_the_pair_lock(ctx: Context) -> None:
    async with ctx.pool.connection() as conn:
        _, pair = await licence.admit(ctx, conn, SimpleNamespace(name="a"), lock=True)  # type: ignore[arg-type]
        assert pair is not None and pair[1]["count"] == 1
        async with ctx.pool.connection() as other:
            cur = await other.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = %s AND granted",
                (0x44534C43,),
            )
            assert (await cur.fetchone())[0] == 1  # type: ignore[index]


@pytest.mark.db
async def test_shown_never_raises(ctx: Context) -> None:
    current, lines = await licence.shown(ctx)
    assert current is not None and current.state == "evaluation"
    assert lines[0].startswith(
        f"Unlicensed evaluation of installation {current.installation_id}: at most 2 pairs (0 used)"
    )
    await ctx.pool.close()
    assert await licence.shown(ctx) == (None, ["licence status unavailable (PoolClosed): see the server log"])


@pytest.mark.db
async def test_a_removed_key_row_logged_once_not_warned(
    ctx: Context, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    removed = PackSigner(ec.generate_private_key(ec.SECP256R1()))
    monkeypatch.setattr(licence, "REMOVED_KEYS", {removed.fingerprint: "2027-01-05"})
    found = licence.parse(licence_file((await _facts(ctx))["installation_id"], key=removed._key),
                          keys=[removed.public_pem])  # fmt: skip
    async with ctx.pool.connection() as conn:  # installed before the release that removed its key
        await repo.insert_licence(conn, found.licence_id, found.text, found.sha256, installed_by=TESTER.actor)
    with caplog.at_level(logging.INFO, logger="deltasleuth.licence"):
        await licensed(ctx)  # its reissue
        said = [await licence.status(ctx) for _ in range(2)]
    why = (
        f"signed by key {removed.fingerprint[:12]}…, withdrawn on 2027-01-05 after a compromise; its reissue"
        " replaces it"
    )
    assert [(r.levelno, r.message) for r in caplog.records if found.licence_id in r.message] == [
        (logging.INFO, f"licence {found.licence_id} in the store does not count: {why}")
    ]
    assert [(body["state"], body["warnings"]) for body in said] == [("valid", [])] * 2
    assert {row["licence_id"]: row["problem"] for row in said[0]["installed"]}[found.licence_id] == why
