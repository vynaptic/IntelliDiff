"""The exact-CPython fallback reuses bounded children and replaces a timed-out one."""

from __future__ import annotations

import os
import select
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

from deltasleuth.core.spec import _keypattern_process as process
from deltasleuth.core.spec.expressions import KeyPatternLimitError, compile_key_pattern


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> Iterator[process._Pool]:
    own = process._Pool()
    monkeypatch.setattr(process, "_POOL", own)
    yield own
    own.close()


def test_fallback_reuses_workers_for_valid_python_semantics(pool: process._Pool) -> None:
    pattern = compile_key_pattern(r"(?u:^\w+$)")
    assert pattern.search("\u00b2") and pattern.search("\u0301") is None
    first = next(iter(pool.workers)).proc.pid
    assert all(pattern.search("customer_123") for _ in range(1000))
    assert [worker.proc.pid for worker in pool.workers] == [first]


def test_timed_out_worker_is_reaped_and_replaced(pool: process._Pool) -> None:
    assert compile_key_pattern(r"(?u:\w+)").search("é")
    failed = next(iter(pool.workers)).proc
    with pytest.raises(KeyPatternLimitError, match="time limit"):
        compile_key_pattern(r"(?u:^(a|aa)+$)").search("a" * 100 + "b")
    assert failed.returncode is not None and not pool.workers
    with pytest.raises(ChildProcessError):
        os.waitpid(failed.pid, os.WNOHANG)
    assert compile_key_pattern(r"(?u:\w+)").search("é")
    assert next(iter(pool.workers)).proc.pid != failed.pid


def test_concurrent_searches_use_at_most_two_workers(pool: process._Pool) -> None:
    pattern = compile_key_pattern(r"(?u:^\w+$)")
    with ThreadPoolExecutor(max_workers=6) as threads:
        assert all(threads.map(pattern.search, ["é"] * 200))
    assert 1 <= len(pool.workers) <= 2
    assert len(pool.idle) == len(pool.workers)


def test_capacity_wait_is_bounded_and_never_a_nonmatch(pool: process._Pool) -> None:
    assert pool.slots.acquire(timeout=0.1) and pool.slots.acquire(timeout=0.1)
    try:
        with pytest.raises(KeyPatternLimitError, match="capacity is busy"):
            compile_key_pattern(r"(?u:\w+)").search("é")
    finally:
        pool.slots.release()
        pool.slots.release()
    assert not pool.workers


def test_frames_preserve_unicode_and_large_inputs(pool: process._Pool) -> None:
    assert compile_key_pattern(r"(?u:^\w+$)").search("é" * 100_000)
    assert compile_key_pattern(r"(?u:^.$)").search("\ud800")


def test_parent_compiler_warns_once_and_worker_does_not_fill_stderr(pool: process._Pool) -> None:
    with pytest.warns(FutureWarning, match="Possible nested set") as seen:
        pattern = compile_key_pattern("prefix[[:alpha:]]")
    assert len(seen) == 1
    assert pattern.search("prefixa]")
    worker = next(iter(pool.workers))
    assert not select.select([worker.proc.stderr], [], [], 0)[0]


def test_workers_keep_running_after_cumulative_cpu_exceeds_a_request_budget(pool: process._Pool) -> None:
    # Each bounded mismatch takes roughly a millisecond; thousands use more than one CPU second.
    # A fixed lifetime RLIMIT_CPU=1 would kill this reused worker even though no search is slow.
    pattern = compile_key_pattern(r"(?u:^(a+)+$)")
    assert pattern.search("a" * 14 + "b") is None
    first = next(iter(pool.workers)).proc.pid
    for _ in range(2500):
        assert pattern.search("a" * 14 + "b") is None
    assert [worker.proc.pid for worker in pool.workers] == [first]


def test_fork_closes_inherited_handles_without_killing_parent_workers() -> None:
    source = """
import os
from deltasleuth.core.spec import _keypattern_process as process
from deltasleuth.core.spec.expressions import compile_key_pattern
pattern = compile_key_pattern(r'(?u:^\\w+$)')
assert pattern.search('é')
parent_worker = next(iter(process._POOL.workers)).proc.pid
child = os.fork()
if child == 0:
    try:
        assert not process._POOL.workers
        assert pattern.search('é')
        assert next(iter(process._POOL.workers)).proc.pid != parent_worker
        process._POOL.close()
    except BaseException:
        os._exit(1)
    os._exit(0)
assert os.waitpid(child, 0)[1] == 0
assert pattern.search('é')
assert next(iter(process._POOL.workers)).proc.pid == parent_worker
process._POOL.close()
"""
    run = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True, timeout=5)
    assert run.returncode == 0, run.stderr


def test_normal_exit_reaps_idle_workers() -> None:
    source = """
from deltasleuth.core.spec import _keypattern_process as process
from deltasleuth.core.spec.expressions import compile_key_pattern
assert compile_key_pattern(r'(?u:\\w+)').search('é')
print(next(iter(process._POOL.workers)).proc.pid)
"""
    run = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True, timeout=5)
    assert run.returncode == 0, run.stderr
    with pytest.raises(ProcessLookupError):
        os.kill(int(run.stdout), 0)
