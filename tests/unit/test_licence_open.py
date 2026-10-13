"""The open build (docs/mvp/LICENCE.md §19): without a vendor key the standing is ``open``, which judges
no stored row, counts no pair, refuses and warns of nothing and records nothing on a run; a file is neither
shown nor installed; ``watch`` audits the state once, and closes an evaluation's; a run made in an
evaluation keeps its own label and gets no pack; a vendor key brings the evaluation back; and the guides
say what an open build prints in its own words."""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deltasleuth import licence
from deltasleuth.context import Context
from deltasleuth.licence import LicenceError, LicenceRefused
from deltasleuth.security.signing import PackSigner
from deltasleuth.store import repo
from tests.conftest import TESTER, VENDOR_KEY, licence_file
from tools import release

from .test_licence import (  # noqa: F401 (ctx: a fixture)
    I1,
    INSTALLATION,
    LICENCE_ID,
    STARTS,
    _facts,
    _written,
    at,
    ctx,
    example,
    judged,
    ran,
    stored,
)

RUN = "R-" + "A" * 26
EVALUATION = {"licence_id": None, "tier": None, "state": "evaluation", "ends": None}  # a run's record
ALL = {"runs": True, "sso": True, "signoff": True, "packs": True}


def vendor(monkeypatch: pytest.MonkeyPatch) -> None:
    """From here the build trusts the tests' vendor key, as a later vendor build does."""
    monkeypatch.setattr(licence, "VENDOR_KEYS", (PackSigner(VENDOR_KEY).public_pem,))


def test_no_vendor_key_is_the_open_standing() -> None:
    assert licence.VENDOR_KEYS == ()  # the session's (``open_build``), whatever the build embeds
    row = stored(example())  # a row a vendor build stored: read by none of this
    found = judged(
        row, day=STARTS, mode="oidc", packs=True, start={"auth": "oidc", "pack_key": "ab" * 32},
        installs=({"id": 812, "object": "LI-" + "0" * 26},), newest=at(STARTS) + timedelta(days=3),
    )  # fmt: skip
    assert (found.state, found.licence, found.installation_id) == ("open", None, INSTALLATION)
    assert (found.limit, found.since, found.seats, found.enterprise, found.sso_fenced) == (
        None, None, [], True, False,
    )  # fmt: skip
    assert [found.refusal(feature) for feature in ("run", "signoff", "packs")] == [None, None, None]
    assert found.allows() == ALL and found.warnings() == [] and found.problems == []
    assert (found.uncounted, found.retired, found.missing, found.clock_behind) == ((), (), (), None)
    assert found.rows == (row,) and found.record() is None
    assert found.unseated("cards") is None and found.paused("cards") is None
    assert found.detail() == {"installation_id": INSTALLATION, "state": "open", "licence_id": None,
                              "tier": None, "pairs": None, "ends": None}  # fmt: skip
    with pytest.raises(ValueError, match="unknown licensed feature 'reveal'"):
        found.refusal("reveal")


def test_a_vendor_key_brings_the_evaluation_back(monkeypatch: pytest.MonkeyPatch) -> None:
    assert judged(day=STARTS, mode="oidc").state == "open"
    vendor(monkeypatch)  # read at each call: the next decision is a vendor build's
    found = judged(day=STARTS, mode="oidc")
    assert (found.state, found.limit, found.sso_fenced) == ("evaluation", 2, True)
    assert found.allows() == {"runs": False, "sso": False, "signoff": False, "packs": False}
    assert found.record() == EVALUATION and found.warnings()[0].startswith("Unlicensed evaluation of")
    licensed = judged(stored(example()), day=STARTS, mode="oidc")
    assert (licensed.state, licensed.licence.licence_id) == ("valid", LICENCE_ID)  # type: ignore[union-attr]


def test_a_file_is_neither_shown_nor_installed() -> None:
    for data in (example(), b"anything"):  # before its grammar: nothing of it is read
        with pytest.raises(LicenceError) as refused:
            licence.parse(data)
        assert str(refused.value) == I1
    assert I1 == (
        "this build embeds no vendor key: it needs no licence and can verify none (docs/mvp/LICENCE.md §19)"
    )


def test_the_guides_quote_the_open_build_word_for_word(repo_root: Path) -> None:
    """What the deployment guide and the runbook say an open build prints is what it prints, line breaks
    aside: ``ds licence``'s lines (``cli/test_cli.py`` holds the command to them), I1 and the Licence
    screen's line; and the release documents give the line its notes must carry as the tool asks for it."""
    screen = "This build needs no licence and limits nothing."
    assert f"return '{screen}';" in (repo_root / "web/src/licensing.ts").read_text()
    printed = ("licence none: this build needs no licence", "state open", f"error: {I1}", screen,
               "allows: runs yes, sso yes, signoff yes, packs yes")  # fmt: skip
    quoted = {"docs/mvp/DEPLOY.md": printed, "docs/mvp/RUNBOOK.md": printed,
              "docs/mvp/RELEASE.md": (release.OPEN_BUILD,), "SECURITY.md": (release.OPEN_BUILD,)}  # fmt: skip
    said = {name: " ".join((repo_root / name).read_text().split()) for name in quoted}
    assert [(name, text) for name, texts in quoted.items() for text in texts if text not in said[name]] == []


def test_a_run_s_own_label_is_kept_and_new_runs_carry_none() -> None:
    new = {"run_id": RUN, "params": {"licence": None}}
    old = {"run_id": RUN, "params": {"licence": EVALUATION}}
    assert licence.run_label(new) is None and licence.file_name(new, "report.pdf") == f"{RUN}-report.pdf"
    assert licence.run_label(old) == (
        f"Unlicensed evaluation: run {RUN} was made without a licence. Not for production use; no evidence"
        " pack is issued for it."
    )
    assert licence.file_name(old, "report.pdf") == f"{RUN}-unlicensed-evaluation-report.pdf"
    # So no pack is issued for it, whose certificate would contradict that label (M8, D8): a new run gets one.
    current = judged(day=STARTS, mode="oidc", packs=True)
    assert licence.pack_problems(current, new) == [] == licence.pack_problems(current, {**new, "params": {}})
    assert licence.pack_problems(current, old) == [
        f"run {RUN} was made in an unlicensed evaluation: no evidence pack is issued for it; start a new run"
    ]


async def _admit(ctx: Context, name: str, *, lock: bool = False) -> Any:  # noqa: F811 (not the fixture)
    async with ctx.pool.connection() as conn:
        return await licence.admit(ctx, conn, SimpleNamespace(name=name), lock=lock)  # type: ignore[arg-type]


@pytest.mark.db
async def test_every_name_is_admitted_and_none_counted(
    ctx: Context,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = [f"pair-{n}" for n in range(1, 11)]  # a third and a tenth among them
    for name in names:
        assert await _admit(ctx, name) == await _admit(ctx, name, lock=True) == (None, None)
        await ran(ctx, name)
    async with ctx.pool.connection() as conn:
        current = await licence.need(ctx, conn, "run", name="pair-10")  # as a licence-paused soak's resume
        for feature in ("signoff", "packs"):
            assert (await licence.need(ctx, conn, feature)).state == "open"
    assert (current.state, current.counted, current.names, current.limit) == ("open", (), [], None)
    assert await licence.stopped(ctx, {"run_id": RUN}, SimpleNamespace(name="pair-3"), current.now) is None  # type: ignore[arg-type]
    assert await _written(ctx) == ([], [])  # no licence, no licence.pair, no other licence row
    # A later vendor build counts what the store holds, the open build's runs too: the first two names ever.
    vendor(monkeypatch)
    async with ctx.pool.connection() as conn:
        counted = await licence.standing(ctx, conn)
    assert (counted.state, counted.names, counted.seats) == ("evaluation", names, names[:2])
    assert (await _admit(ctx, "pair-1"))[1] is None
    with pytest.raises(LicenceRefused, match=r"^spec pair-3 is not one of the 2 pairs an unlicensed"):
        await _admit(ctx, "pair-3")
    with pytest.raises(LicenceRefused, match=r"^spec pair-11 would be a third pair"):
        await _admit(ctx, "pair-11", lock=True)


@pytest.mark.db
async def test_status_and_shown_say_nothing_of_a_licence(
    ctx: Context,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    installation = (await _facts(ctx))["installation_id"]
    vendor(monkeypatch)  # a vendor build stored a licence on this store before
    found = licence.parse(licence_file(installation))
    async with ctx.pool.connection() as conn:
        await repo.insert_licence(conn, found.licence_id, found.text, found.sha256, installed_by=TESTER.actor)
    monkeypatch.setattr(licence, "VENDOR_KEYS", ())
    await ran(ctx, "customer-profile")
    said: list[str] = []
    with caplog.at_level(logging.INFO, logger="deltasleuth.licence"):
        current, lines = await licence.shown(ctx, said.append)
        status = await licence.status(ctx)
    assert current is not None and (current.state, lines, said, caplog.messages) == ("open", [], [], [])
    (row,) = status.pop("installed")
    assert status == {
        "installation_id": installation, "state": "open", "warnings": [], "licence": None, "renewal": None,
        "allows": ALL, "pairs": {"limit": None, "since": None, "counted": []}, "clock_behind_seconds": None,
    }  # fmt: skip
    assert (row["licence_id"], row["tier"], row["starts"], row["ends"], row["in_force"], row["problem"]) == (
        found.licence_id, None, None, None, False, I1,
    )  # fmt: skip
    with pytest.raises(LicenceError) as refused:
        await licence.install(ctx, licence_file(installation), who=TESTER)
    assert str(refused.value) == I1
    assert [action for action, _, _ in (await _written(ctx))[1]] == []  # nothing written, nothing audited


@pytest.mark.db
async def test_watch_audits_the_open_state_once_and_closes_an_evaluation_s(
    ctx: Context,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    installation = (await _facts(ctx))["installation_id"]
    vendor(monkeypatch)
    await licence.watch(ctx)  # as the older build's worker recorded it
    monkeypatch.setattr(licence, "VENDOR_KEYS", ())
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="deltasleuth.licence"):
        await licence.watch(ctx)
        await licence.watch(ctx)  # the next hour's: nothing changed, nothing recorded
    said = [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "deltasleuth.licence"]
    assert said == [("INFO", "licence state evaluation -> open (None)")]  # the audit copy is another logger's
    vendor(monkeypatch)
    await licence.watch(ctx)  # and a later vendor build's
    states = [(obj, detail) for action, obj, detail in (await _written(ctx))[1] if action == "licence.state"]
    assert states == [
        (installation, {"from": was, "to": to, "licence_id": None, "ends": None})
        for was, to in ((None, "evaluation"), ("evaluation", "open"), ("open", "evaluation"))
    ]
