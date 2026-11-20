"""Judge processes (``worker.judge_processes``): a group judged in a process that holds no key is judged as
a thread of the worker judges it, a judge that ends fails only the key that ended it, and the worker leaves
no judge running as it stops."""

from __future__ import annotations

import json
import os
import pickle
import signal
import subprocess
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deltasleuth.core.judge import compared, judge_keyless, observed
from deltasleuth.core.masking import Tokeniser
from deltasleuth.core.spec import compile_spec
from deltasleuth.core.spec.documents import parse_yaml_text
from deltasleuth.core.types import (
    AuthoritativeAbsence,
    KeyResult,
    Observation,
    Side,
    SideStateKind,
    TargetCapabilities,
)
from deltasleuth.runner import _judge_process, compare, create_run, judges, worker
from deltasleuth.store import repo
from tests.conftest import TESTER

from .conftest import TARGETS, UNAVAILABLE, OpenHarness, ScriptedService, doc, spec_text

TODAY = date(2026, 11, 20)


def known(tokeniser: Tokeniser) -> str:
    """Two known differences: a paused item reads as active on side b, and limit 12345 differs, a rule
    written against the finding's pointer as it is shown, masked with ``tokeniser``."""
    pointer = "/limits/" + tokeniser.token("free_text", "12345")
    return f"""knownDiffs:
  - id: KD-0001
    when: "path == '$.status' && a == 'PAUSED'"
    classification: expected_by_design
    reason: a paused item reads as active on side b
    ticket: TCK-0001
  - id: KD-0002
    when: "pointer == '{pointer}' && change_type == 'value_changed'"
    classification: expected_by_design
    reason: side b raised one limit on purpose
    ticket: TCK-0002
"""


TOKENISER = Tokeniser(b"an invented tokeniser key of 32+ bytes")


def _spec(tokeniser: Tokeniser, keys: list[str] | None = None) -> str:
    text = spec_text(keys, extra=known(tokeniser))
    return text.replace("  fields:\n", '  fields:\n    - {path: "$.limits"}\n')


SPEC = compile_spec(parse_yaml_text(_spec(TOKENISER)))
CAPS = {t: TargetCapabilities(authoritative_absence=AuthoritativeAbsence((404,))) for t in ("svc-a", "svc-b")}


def _doc(key: str, **fields: Any) -> dict[str, Any]:
    """The unit document with ``limits``, whose member names read as data: a pointer through one is
    masked with the tokeniser."""
    return doc(key) | {"limits": {"12345": 1}} | fields


def _read(key: str, side: Side, body: Any, status: int = 200) -> Observation:
    return Observation(key, side, status, json.dumps(body).encode(), "2026-11-20T08:00:00Z")


def _items() -> list[judges.Item]:
    """K001 the same on both sides, K002 a known difference, K003 one through a protected member name,
    K004 not found on side b, K005 side b failing."""
    pairs = {
        "K001": (_doc("K001"), _doc("K001"), 200),
        "K002": (_doc("K002", status="PAUSED"), _doc("K002"), 200),
        "K003": (_doc("K003"), _doc("K003", limits={"12345": 2}), 200),
        "K004": (_doc("K004"), {"error": "NOT_FOUND"}, 404),
        "K005": (_doc("K005"), UNAVAILABLE[1], UNAVAILABLE[0]),
    }
    return [
        (key, {Side.A: _read(key, Side.A, a), Side.B: _read(key, Side.B, b, status)}, {})
        for key, (a, b, status) in pairs.items()
    ]


def _thread(item: judges.Item) -> KeyResult:
    """A key judged as a thread of the worker judges it."""
    key, reads, states = item
    return compared(SPEC, key, observed(SPEC, CAPS, reads, states), today=TODAY, tokeniser=TOKENISER)


def _finished(item: judges.Item, answer: judges.Judged) -> KeyResult:
    key, reads, states = item
    ctx: Any = SimpleNamespace(tokeniser=TOKENISER)
    return compare._finished(ctx, SPEC, CAPS, key, reads, states, answer, TODAY)


def test_a_comparison_that_asks_the_tokeniser_goes_back_as_the_key_s_states() -> None:
    """Without the key a judge finishes every key but one whose finding's pointer crosses a protected
    member name (a known-difference rule reads the pointer masked): that key's states go back."""
    answers = {item[0]: judge_keyless(SPEC, CAPS, *item, today=TODAY) for item in _items()}
    assert {key: type(answer).__name__ for key, answer in answers.items()} == {
        "K001": "KeyResult", "K002": "KeyResult", "K003": "dict", "K004": "KeyResult", "K005": "KeyResult",
    }  # fmt: skip
    for item in _items():
        assert _finished(item, answers[item[0]]) == _thread(item)
    assert [_thread(item).outcome.value for item in _items()] == [
        "MATCH", "KNOWN_DIFF", "KNOWN_DIFF", "NOT_FOUND_B", "INCONCLUSIVE",
    ]  # fmt: skip


async def test_a_judge_process_judges_as_a_thread_does() -> None:
    pool = judges.Judges(2)
    try:
        items = _items()
        answers = await pool.judge(SPEC, CAPS, items, TODAY)
        assert [_finished(item, answer) for item, answer in zip(items, answers, strict=True)] == [
            _thread(item) for item in items
        ]
        assert isinstance(answers[2], dict) and answers[2][Side.B].kind is SideStateKind.COMPLETE
        assert len(pool.pids) == 1  # started as groups need them
    finally:
        pool.close()
    assert pool.pids == []


async def test_a_spec_is_pickled_once_and_sent_once_to_each_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[bool] = []
    dumps, exchange = pickle.dumps, judges._Judge._exchange
    pickled: list[str] = []

    def counting(obj: Any, *args: Any, **kwargs: Any) -> bytes:
        if obj is SPEC:
            pickled.append(SPEC.content_hash)
        return dumps(obj, *args, **kwargs)

    def recording(self: Any, content_hash: str, spec: bytes | None, *args: Any) -> Any:
        sent.append(spec is not None)
        return exchange(self, content_hash, spec, *args)

    monkeypatch.setattr(judges.pickle, "dumps", counting)
    monkeypatch.setattr(judges._Judge, "_exchange", recording)
    pool = judges.Judges(1)
    try:
        for _ in range(3):
            await pool.judge(SPEC, CAPS, _items()[:2], TODAY)
        assert (sent, pickled) == ([True, False, False], [SPEC.content_hash])
        # A judge that gave the spec up says so, and is sent it again.
        (judge,) = pool._idle
        judge.pinned.clear()
        judge.pinned["sha256:an-invented-hash"] = None
        other = replace(SPEC, content_hash="sha256:an-invented-hash")
        answers = await pool.judge(other, CAPS, _items()[:1], TODAY)
        assert sent[3:] == [False, True] and isinstance(answers[0], KeyResult)
    finally:
        pool.close()


def _ending_on(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    """Each judge's process is killed as it is asked to judge a group holding ``key``."""
    exchange = judges._Judge._exchange

    def ending(self: Any, content_hash: str, spec: bytes | None, caps: Any, today: date, items: Any) -> Any:
        if key in [item[0] for item in items]:
            os.kill(self.pid, signal.SIGKILL)
        return exchange(self, content_hash, spec, caps, today, items)

    monkeypatch.setattr(judges._Judge, "_exchange", ending)


async def test_a_judge_that_ends_fails_only_the_key_that_ended_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _ending_on(monkeypatch, "K002")
    pool = judges.Judges(1)
    try:
        items = _items()
        answers = await pool.judge(SPEC, CAPS, items, TODAY)
        assert answers[1] is judges.Ended.JUDGE
        for i in (0, 2, 3, 4):
            assert _finished(items[i], answers[i]) == _thread(items[i])
        ended = _finished(items[1], answers[1])
        assert (ended.outcome.value, ended.inconclusive_reason) == ("INCONCLUSIVE", "resource_limit")
        assert ended.side_a.detail == judges.ENDED_DETAIL == ended.side_b.detail
        assert len(pool.pids) == 1  # a judge that ended is replaced
    finally:
        pool.close()


async def test_a_judge_that_answers_outside_the_protocol_leaves_its_group_to_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refused(*_: Any) -> Any:
        raise judges.JudgeAnswered("an invented answer")

    monkeypatch.setattr(judges, "_checked", refused)
    pool = judges.Judges(1)
    try:
        items = _items()
        answers = await pool.judge(SPEC, CAPS, items, TODAY)
        assert answers == [None] * len(items) and pool.pids == []  # the judge is ended
        assert [_finished(item, None) for item in items] == [_thread(item) for item in items]
    finally:
        pool.close()


def test_an_answer_holds_the_engine_s_types_alone() -> None:
    result = _thread(_items()[2])
    assert judges.loads(pickle.dumps([result, None])) == [result, None]
    for foreign in (os.system, subprocess.Popen, Path):
        with pytest.raises(pickle.UnpicklingError, match="a judge answered with"):
            judges.loads(pickle.dumps(foreign))


def test_a_judge_starts_with_no_variable_no_argument_and_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A judge is this interpreter on the judge's file, isolated, with no variable of this process; what it
    imports is the engine alone: no settings, keyring, tokeniser key or evidence store."""
    started: list[tuple[Any, dict[str, Any]]] = []
    popen = subprocess.Popen

    def recording(args: Any, **kwargs: Any) -> Any:
        started.append((args, kwargs))
        return popen(args, **kwargs)

    monkeypatch.setattr(judges.subprocess, "Popen", recording)
    judge = judges._Judge()
    try:
        judge.ready()
    finally:
        judge.end()
    ((args, kwargs),) = started
    assert args == [sys.executable, "-I", "-X", "utf8", _judge_process.__file__]
    assert kwargs["env"] == {} and kwargs["stderr"] == subprocess.DEVNULL and kwargs["start_new_session"]

    probe = (  # the judge's own file loads its package, then what the judge imports
        "import importlib.util, json, sys; "
        f"found = importlib.util.spec_from_file_location('judge', {_judge_process.__file__!r}); "
        "judge = importlib.util.module_from_spec(found); found.loader.exec_module(judge); judge._package(); "
        "import deltasleuth.core.judge; print(json.dumps(sorted(sys.modules)))"
    )
    loaded = subprocess.run(
        [sys.executable, "-I", "-c", probe], env={}, capture_output=True, text=True, check=True, cwd=os.sep
    )
    modules = json.loads(loaded.stdout)
    ours = [m for m in modules if m.split(".")[0] == "deltasleuth"]
    engine = ("deltasleuth.core", "deltasleuth.errors", "deltasleuth.packs")
    assert ours and [m for m in ours if m != "deltasleuth" and not m.startswith(engine)] == []
    assert not {"psycopg", "psycopg_pool", "httpx", "aiohttp"} & {m.split(".")[0] for m in modules}


# -- through the worker -----------------------------------------------------------------------------------


def _script(service: ScriptedService) -> list[str]:
    """Keys of every kind, through the scripted service; a key that differs is read again (``recheck``)."""
    service.both("K001", _doc("K001"), _doc("K001"))
    service.both("K002", _doc("K002", status="PAUSED"), _doc("K002"))
    service.both("K003", _doc("K003"), _doc("K003", limits={"12345": 2}))
    service.script("a", "K004", (200, _doc("K004")))
    service.script("a", "K005", (200, _doc("K005")))
    service.script("b", "K005", UNAVAILABLE)
    service.both("K006", _doc("K006", name="Alice"), _doc("K006", name=" Alice "))  # INFO: normalised
    service.both("K007", _doc("K007", tags=["x"]), _doc("K007", tags=["x", "z"]))
    service.script("a", "K008", (200, _doc("K008")))
    service.script("b", "K008", (200, _doc("K008", status="CLOSED")), (200, _doc("K008")))  # converges
    return [f"K00{i}" for i in range(1, 9)]


async def _saved(h: Any, run_id: str) -> tuple[list[Any], list[Any], list[Any]]:
    """A run's results, findings and root causes, without what names the run or its rows."""
    async with h.ctx.pool.connection() as conn:
        results = await repo.pages(repo.list_results, conn, run_id)
        findings = await repo.pages(repo.list_findings, conn, run_id)
        causes = await repo.list_root_causes(conn, run_id)
    apart = {"run_id", "obs_a_id", "obs_b_id", "finding_id", "root_cause_id", "created_at", "updated_at"}

    def kept(rows: list[Any]) -> list[Any]:
        return sorted(
            (json.dumps({k: v for k, v in dict(row).items() if k not in apart}, sort_keys=True, default=str)
             for row in rows),
        )  # fmt: skip

    return kept(results), kept(findings), kept(causes)


async def test_judge_processes_save_what_a_thread_saves(
    open_harness: OpenHarness, service: ScriptedService, monkeypatch: pytest.MonkeyPatch
) -> None:
    answered: list[str] = []
    judge = judges.Judges.judge

    async def recording(self: Any, *args: Any) -> Any:
        answers = await judge(self, *args)
        answered.extend(type(answer).__name__ for answer in answers)
        return answers

    monkeypatch.setattr(judges.Judges, "judge", recording)
    keys = _script(service)
    assert service.server is not None
    targets = TARGETS.format(base=f"http://127.0.0.1:{service.server.port}", rate=500)
    saved = []
    for processes in (0, 2):
        service.calls.clear()  # each run reads each key's script from its start
        async with open_harness(targets, judge_processes=processes) as h:
            spec_id = await h.spec(_spec(h.ctx.tokeniser, keys))
            run_id = await create_run(h.ctx, spec_id, "batch", who=TESTER)
            assert (await h.finish(run_id))["status"] in ("completed", "completed_with_issues")
            saved.append(await _saved(h, run_id))
    thread, processes_ = saved
    assert thread == processes_
    # The processes judged every key, results and states alike: none was left to the worker.
    assert {"KeyResult", "dict"} == set(answered)
    rows = [json.loads(row) for row in thread[0]]
    assert {row["key_masked"]: row["outcome"] for row in rows} == {
        "K001": "MATCH", "K002": "KNOWN_DIFF", "K003": "KNOWN_DIFF", "K004": "NOT_FOUND_B",
        "K005": "INCONCLUSIVE", "K006": "MATCH", "K007": "MISMATCH", "K008": "DRIFT",
    }  # fmt: skip
    assert any(row["confirmation"] == "rechecked" for row in rows)  # first reads judged again
    assert thread[1] and thread[2]  # findings and root causes


async def test_a_key_whose_judging_ends_a_judge_is_inconclusive_and_the_run_goes_on(
    open_harness: OpenHarness, service: ScriptedService, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = _script(service)[:3]
    _ending_on(monkeypatch, "K002")
    assert service.server is not None
    targets = TARGETS.format(base=f"http://127.0.0.1:{service.server.port}", rate=500)
    async with open_harness(targets, judge_processes=1) as h:
        spec_id = await h.spec(_spec(h.ctx.tokeniser, keys))
        run_id = await create_run(h.ctx, spec_id, "batch", who=TESTER)
        assert (await h.finish(run_id))["status"] in ("completed", "completed_with_issues")
        results = await h.results(run_id)
    # K002 is INCONCLUSIVE on its first read and on its recheck, each side resource_limit.
    assert {key: (r["outcome"], r["side_a_reason"], r["side_b_reason"]) for key, r in results.items()} == {
        "K001": ("MATCH", None, None),
        "K002": ("INCONCLUSIVE", "resource_limit", "resource_limit"),
        "K003": ("KNOWN_DIFF", None, None),
    }


async def test_the_worker_ends_its_judges_as_it_stops(
    open_harness: OpenHarness, service: ScriptedService, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: set[int] = set()

    class Recorded(judges.Judges):
        async def judge(self, *args: Any) -> Any:
            answers = await super().judge(*args)
            seen.update(self.pids)  # the judges running once a group is judged
            return answers

    monkeypatch.setattr(worker, "Judges", Recorded)
    keys = _script(service)
    assert service.server is not None
    targets = TARGETS.format(base=f"http://127.0.0.1:{service.server.port}", rate=500)
    async with open_harness(targets, judge_processes=2) as h:
        spec_id = await h.spec(_spec(h.ctx.tokeniser, keys))
        run_id = await create_run(h.ctx, spec_id, "batch", who=TESTER)
        await h.finish(run_id)
    assert seen  # the run was judged by judge processes
    for pid in seen:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)  # ended and reaped
