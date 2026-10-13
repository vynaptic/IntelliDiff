"""The benchmark harness, with no database: CliRunner, a stub docker and a stub ds
on PATH that follow a scenario and log each run's argv and environment, as tests/unit/releases.py's do. The
runs that need PostgreSQL are tests/e2e/test_bench.py's."""

from __future__ import annotations

import getpass
import hashlib
import itertools
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict
from typer.testing import CliRunner, Result

from tools import bench, release

STUB = r"""#!__PYTHON__ -S
# A stub of docker or ds for tools/bench.py's tests: it follows the scenario, keeps the containers it runs in
# the state file, and logs each run's argv and environment.
import json, os, sys, time
from pathlib import Path

CONFIG = __CONFIG__
tool, args = Path(sys.argv[0]).name, sys.argv[1:]
scenario = json.loads(Path(CONFIG["scenario"]).read_text())
state = json.loads(Path(CONFIG["state"]).read_text())
added = ("__CF_USER_TEXT_ENCODING", "LC_CTYPE")  # what macOS and Python's locale coercion add to this one
with open(CONFIG["log"], "a") as log:
    log.write(json.dumps({"tool": tool, "argv": args, "env": {k: v for k, v in os.environ.items()
                                                              if k not in added}}) + "\n")


def done(out="", code=0, err=""):
    Path(CONFIG["state"]).write_text(json.dumps(state))
    sys.stdout.write(out)
    sys.stderr.write(err)
    sys.exit(code)


if tool == "docker":
    named = state["containers"] + scenario.get("existing", [])
    if args[:2] == ["context", "inspect"]:
        done(scenario["endpoint"] + "\n")
    if args[:2] == ["image", "inspect"]:
        done(scenario["image"] + "\n") if scenario["image"] else done(code=1, err="Error: No such image\n")
    if args[0] == "ps":
        done("".join(f"{name}\n" for name in named))
    if args[0] == "run":
        source = args[args.index("-v") + 1].split(":")[0]
        state["password"] = Path(source).read_text().strip()
        state["containers"].append(args[args.index("--name") + 1])
        done("0123abcd\n")
    if args[0] == "exec" and args[1] not in state["containers"]:
        done(code=1, err=f"Error: No such container: {args[1]}\n")
    if args[0] == "exec" and "df" in args:
        free = scenario["df"].pop(0) if len(scenario["df"]) > 1 else scenario["df"][0]
        Path(CONFIG["scenario"]).write_text(json.dumps(scenario))
        done(f"Filesystem 1024-blocks Used Available Capacity Mounted on\noverlay 1 1 {free} 1% /data\n")
    if args[0] == "exec" and "cat" in args:
        done("usage_usec 1000\n")
    if args[0] == "exec":
        done()
    if args[0] == "restart":
        done(args[-1] + "\n")
    if args[0] == "logs":
        done(err=scenario.get("logs", ""))
    if args[0] == "inspect":
        done(scenario.get("state", "false true 0") + "\n")
    if args[0] == "stats":
        done("".join(f"{line}\n" for line in scenario.get("stats", [])))
    if args[0] == "info":
        done(json.dumps({"ServerVersion": "29.1.3", "NCPU": 4, "MemTotal": 8 * 10**9, "CgroupVersion": "2",
                         "OperatingSystem": "Docker Desktop", "Name": "docker-desktop"}))
    if args[0] == "rm":
        state["containers"] = [name for name in state["containers"] if name not in args]
        done()
if tool == "ds":
    if args[0] == "run":
        for line in scenario.get("run", []):
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
            time.sleep(0.05)
        time.sleep(scenario.get("sleep", 0))
        if scenario.get("signal"):
            os.kill(os.getpid(), scenario["signal"])
        done(code=scenario.get("exit", 0))
    done("ds says this\n")
done(code=99, err=f"the {tool} stub has no answer for {args}\n")
"""
SCENARIO = {"endpoint": "unix:///var/run/docker.sock", "image": "sha256:" + "a" * 64, "df": [10**9]}


class Stubs:
    def __init__(self, tmp: Path) -> None:
        self.tmp, self.bin = tmp, tmp / "bin"
        self.out, self.work = tmp / "out", tmp / "work"
        for folder in (self.bin, self.out, self.work):
            folder.mkdir()
        self.scenario(**SCENARIO)
        self.write_state({"containers": [], "password": None})
        config = json.dumps({name: str(tmp / f"{name}.json") for name in ("scenario", "state")}
                            | {"log": str(tmp / "log.jsonl")})  # fmt: skip
        for tool in ("docker", "ds"):
            (self.bin / tool).write_text(
                STUB.replace("__PYTHON__", sys.executable).replace("__CONFIG__", config)
            )
            (self.bin / tool).chmod(0o755)

    def scenario(self, **values: Any) -> None:
        current = (
            json.loads((self.tmp / "scenario.json").read_text())
            if (self.tmp / "scenario.json").exists()
            else {}
        )
        (self.tmp / "scenario.json").write_text(json.dumps(current | values))

    def write_state(self, state: dict[str, Any]) -> None:
        (self.tmp / "state.json").write_text(json.dumps(state))

    def state(self) -> dict[str, Any]:
        return json.loads((self.tmp / "state.json").read_text())

    def runs(self, tool: str = "docker") -> list[dict[str, Any]]:
        log = self.tmp / "log.jsonl"
        lines = log.read_text().splitlines() if log.exists() else []
        return [run for run in map(json.loads, lines) if run["tool"] == tool]

    def bench(self, *args: str, rows: str = "10k") -> Result:
        argv = [
            "run",
            "--out",
            str(self.out),
            "--work",
            str(self.work),
            "--rows",
            rows,
            "--repeat",
            "1",
            *args,
        ]
        return CliRunner().invoke(bench.app, argv)

    def left(self) -> list[Path]:
        return [*self.out.iterdir(), *self.work.iterdir()]


@pytest.fixture
def stubs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Stubs:
    """A stub docker first on PATH, then the system's tools (git, ps, sysctl), and a stub ds beside a
    ``sys.executable`` in the same folder."""
    made = Stubs(tmp_path)
    monkeypatch.setenv("PATH", f"{made.bin}{os.pathsep}/usr/bin:/bin:/usr/sbin:/sbin")
    monkeypatch.setattr(sys, "executable", str(made.bin / "python"))
    return made


def repetition(cache: str = "warm", n: int = 2, *, run: float = 10.0, scan: float = 6.0, first: float = 2.0,
               tail: float = 3.0, cycle_end: float = 1.0, rss: float = 2e8, rows: int = 100_000,
               differing: int = 1000, correct: bool = True, analysed: float | None = -0.5,
               **noise: Any) -> dict[str, Any]:  # fmt: skip
    """A repetition as bench.repetition records one, each store table last analysed, by autovacuum,
    ``analysed`` seconds after the cycle end began (None: never)."""
    observed = dict.fromkeys(bench.SCANNED, 0) | {"rows_a": rows, "rows_b": rows, "candidates": differing}
    quiet = {"quiet_wait_s": 0.0, "load_1m": 1.0, "power": "ac", "others": {"containers": 0, "cpu_pct": 0.0}}
    seconds = {"run": run, "first_progress": first, "scan": scan, "l1": None, "l2": None, "reads_tail": tail,
               "cycle_end": cycle_end, "process": run + 1}  # fmt: skip
    rate = differing / tail if differing >= bench.MIN_KEYS else None
    return {
        "n": n, "cache": cache, "restart_clean": True if cache == "restarted" else None, "correct": correct,
        "problems": [] if correct else ["MISMATCH 1, truth 0"], "aborted": None, "exit": 0,
        "status": "completed", "noise": quiet | {"slept_s": 0.0} | noise,
        "observed": observed | {"snapshots_a": 1, "snapshots_b": 1}, "seconds": seconds,
        "reads": {"keys": differing, "span_s": 1.0, "keys_per_s": None, "open_at_scan_end": differing,
                  "tail_keys_per_s": rate},
        "memory": {"peak_rss_bytes": rss, "rss_scan_end_bytes": rss}, "cpu": {"ds_s": 1.0, "database_s": 0.5},
        "sources": {s: dict.fromkeys(bench.STATS, 0) for s in "ab"}, "temp_peak_bytes": 0,
        "stored": {"store_bytes": 1000 * differing, "evidence_bytes": 500 * differing,
                   "evidence_files": 2 * differing},
        "statistics": {"finished_s": run - cycle_end, "tables": {
            t: {"analyze_s": None, "autoanalyze_s": analysed} for t in bench.STORE}},
    }  # fmt: skip


HOST = {"os": "darwin", "model": "Mac15,9", "cpu": "Apple M3 Max", "cpus": 16, "memory_bytes": 68719476736,
        "python": "3.13.2", "psycopg": "3.3.6", "libpq": "18.6"}  # fmt: skip
DOCKER = {"version": "29.1.3", "cpus": 16, "memory_bytes": 17 * 10**9, "cgroup": "2", "os": "Docker Desktop"}
POSTGRES = {"image": bench.POSTGRES, "image_id": "sha256:" + "a" * 64, "version": "16.14",
            "settings": dict(bench.STOCK)}  # fmt: skip
ENVIRONMENT = {"host": HOST, "docker": DOCKER, "postgres": POSTGRES, "topology": "container", "rtt_ms": 0.2}


def result(sizes: dict[int, list[dict[str, Any]]], scenario: str = "dense", *, sha: str = "0" * 40,
           host: dict[str, Any] | None = None, dirty: bool = False) -> dict[str, Any]:  # fmt: skip
    """A deltasleuth-bench/1 result of one scenario at ``sizes``, each with its repetitions."""
    entries = []
    for rows, reps in sizes.items():
        tables = {"users.a": {"rows": rows, "columns": 4, "table_bytes": 70 * rows, "index_bytes": 30 * rows,
                              "row_bytes": 70, "generated_s": 1.0}}  # fmt: skip
        record = {"scenario": scenario, "truth": bench.truth(scenario, rows), "repetitions": reps,
                  "summary": bench.summary(reps)}  # fmt: skip
        entries.append(bench._entry(rows) | {"tables": tables, "scenarios": [record], "fits_in_memory": True})
    options = {"rows": list(sizes), "scenarios": [scenario], "shape": "narrow",
               "repeat": max(len(r) for r in sizes.values()), "server": False, "timeout_min": 240,
               "stall_min": 10, "max_rss_bytes": 10**10}  # fmt: skip
    made = {
        "format": bench.FORMAT, "bench_id": "0123abcd", "complete": True, "started": "2026-09-30T10:00:00Z",
        "ended": "2026-09-30T11:00:00Z", "publishable": True, "problems": [],
        "commit": {"sha": sha, "dirty": dirty}, "version": "0.1.0", "uv_lock_sha256": "0" * 64,
        "options": options, "environment": ENVIRONMENT | {"host": host or HOST},
        "profile": bench._profile(10**8), "sizes": entries, "notes": [],
    }  # fmt: skip
    made["complete"] = bench._complete(made)
    return made


def saved(tmp: Path, name: str, made: dict[str, Any]) -> Path:
    path = tmp / f"bench-{name}.json"
    path.write_text(json.dumps(made))
    return path


def report(*args: Any) -> Result:
    return CliRunner().invoke(bench.app, ["report", *map(str, args)])


def scratch_repo(root: Path) -> None:
    root.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "o@x.test"],
        ["config", "user.name", "O"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True)
    for name in ("deltasleuth/x.py", "tools/y.py", "pyproject.toml", "uv.lock", "docs/z.md"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("x\n")
    commit(root)


def commit(root: Path) -> str:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "a change"], cwd=root, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
    ).stdout.strip()


def test_sizes_parse() -> None:
    assert bench.sizes("100k,1M,10M") == [100_000, 1_000_000, 10_000_000]
    assert bench.sizes("1M,20000,10k") == [10_000, 20_000, 1_000_000]
    assert bench.sizes("1000M") == [10**9]
    for given in ("5k", "1001M", "0", "15k", "1G", "", "10k,10000", "1M,1000k", "01k"):
        with pytest.raises(bench.typer.Exit):
            bench.sizes(given)
    done = CliRunner().invoke(bench.app, ["run", "--out", ".", "--rows", "10k,15k"])
    assert (done.exit_code, done.stderr) == (
        2, "error: --rows 15k: sizes are multiples of 10,000 from 10,000 to 1,000,000,000, each once\n"
    )  # fmt: skip


def test_truth_closed_forms() -> None:
    sparse = bench.truth("sparse", 1_000_000)
    assert (sparse["MISMATCH"], sparse["NOT_FOUND_B"], sparse["NOT_FOUND_A"]) == (
        10,
        1,
        0,
    )  # the DS_PERF test's
    assert (sparse["matched"], sparse["candidates"], sparse["rows_b"]) == (999_989, 11, 999_999)
    for scenario in ("dense", "clustered"):
        found = bench.truth(scenario, 20_000)
        assert (found["MISMATCH"], found["NOT_FOUND_B"], found["NOT_FOUND_A"]) == (160, 20, 20)
        assert (found["candidates"], found["matched"], found["MATCH"], found["rows_b"]) == (
            200,
            19_820,
            19_820,
            20_000,
        )
    graph = bench.truth("graph", 1_000_000)
    assert (graph["NOT_FOUND_A"], graph["NOT_FOUND_B"], graph["candidates"], graph["matched"]) == (
        100,
        100,
        200,
        999_900,
    )
    assert (graph["groups_different"], graph["rearrange"], graph["split"], graph["merge"]) == (200, 100, 0, 0)
    assert graph["groups_a"] == graph["groups_b"] == 750_001
    for n in (10_000, 20_000, 1_000_000, 10**8):
        assert bench.truth("graph", n)["NOT_FOUND_A"] == n // 10_000  # M = N/10,000
        found = bench.truth("dense", n)
        assert found["matched"] + found["MISMATCH"] + found["NOT_FOUND_B"] == found["rows_a"] == n


def _brute(scenario: str, n: int) -> dict[str, int]:
    """The truth by brute force: side b row by row from Python transcriptions of the predicates, then the
    merge."""
    if scenario == "graph":
        a = {
            (e, t, v) for e in range(1, n // 2 + 1) for t, v in (("phone", f"p{e}"), ("email", f"x{e // 2}"))
        }
        b = {(e, t, f"x{e // 2 + (e % 5000 == 2500)}" if t == "email" else v) for e, t, v in a}
        groups = {side: Counter((t, v) for _, t, v in facts) for side, facts in (("a", a), ("b", b))}
        members = {side: {} for side in "ab"}
        for side, facts in (("a", a), ("b", b)):
            for e, t, v in facts:
                members[side].setdefault((t, v), set()).add(e)
        different = sum(members["a"].get(g) != members["b"].get(g) for g in {*members["a"], *members["b"]})
        return {"rows_a": len(a), "rows_b": len(b), "NOT_FOUND_B": len(a - b), "NOT_FOUND_A": len(b - a),
                "matched": len(a & b), "groups_a": len(groups["a"]), "groups_b": len(groups["b"]),
                "groups_different": different}  # fmt: skip
    selected = {
        "sparse": lambda g: g % 100_000 == 0 or g == 3,
        "dense": lambda g: g % 100 == 50,
        "clustered": lambda g: (g - 1) % (n // 10) < n // 1000,
    }[scenario]
    kind = {
        "sparse": lambda g: 0 if g == 3 else 2,
        "dense": lambda g: (g // 100) % 10,
        "clustered": lambda g: g % 10,
    }
    b: dict[int, str] = {}
    for g in range(1, n + 1):
        k = kind[scenario](g) if selected(g) else None
        if k == 0:
            continue
        b[g] = "changed" if k is not None and k >= 2 else "plain"
        if k == 1:
            b[n + g] = "plain"
    return {"rows_a": n, "rows_b": len(b), "MISMATCH": sum(v == "changed" for v in b.values()),
            "NOT_FOUND_B": sum(g not in b for g in range(1, n + 1)), "NOT_FOUND_A": sum(g > n for g in b),
            "matched": sum(b.get(g) == "plain" for g in range(1, n + 1))}  # fmt: skip


@pytest.mark.parametrize("n", [10_000, 20_000, 30_000, 120_000])
def test_truth_matches_brute_force(n: int) -> None:
    for scenario in bench.SCENARIOS:
        brute, closed = _brute(scenario, n), bench.truth(scenario, n)
        assert {k: closed[k] for k in brute} == brute, scenario


def test_summary_and_noisy() -> None:
    reps = [repetition("restarted", 1, run=20.0), repetition(run=10.0), repetition(n=3, run=10.5),
            repetition(n=4, run=99.0, correct=False)]  # fmt: skip
    found = bench.summary(reps)
    assert set(found) == {"restarted", "warm"}
    assert found["warm"]["n"] == 2 and found["warm"]["run_s"] == {"median": 10.25, "min": 10.0, "max": 10.5}
    assert (
        found["warm"]["noisy"] is False
    )  # the restart's 20 s is its own state, and the incorrect one no number
    assert found["restarted"] == found["restarted"] | {"n": 1, "noisy": False}
    assert bench._headline({"summary": found}) == ("warm", found["warm"])
    assert bench._headline({"summary": {"restarted": found["restarted"]}})[0] == "restarted"  # the one state
    assert bench.summary([repetition(run=10.0), repetition(run=12.0)])["warm"]["noisy"] is True
    assert "keys_per_s" not in found["warm"]  # a null rate is no number
    assert found["warm"]["stored_per_key"]["median"] == 1500


def test_statistics_states_are_never_pooled(tmp_path: Path) -> None:
    """Each repetition records when its store's tables were last analysed against finished_at, the cycle
    end's start (run 10 s, cycle end 1 s); repetitions whose store had other statistics are states apart."""
    kinds = [bench._analysed(repetition(analysed=at)) for at in (-0.5, 0.5, 5.0, None)]
    assert kinds == ["analysed", "analysed during the cycle end", "unanalysed", "unanalysed"]  # 5 s: after it
    partly = repetition(analysed=None)
    partly["statistics"]["tables"]["run_keys"]["autoanalyze_s"] = -3.0
    assert bench._analysed(partly) == "unanalysed: findings, observations, results; analysed: run_keys"
    # The product analyses the findings again while the cycle end links their root causes, a table that
    # had statistics: analysed before it began, unless autovacuum analysed them during the cycle end too.
    linked = repetition()
    linked["statistics"]["tables"]["findings"] = {"analyze_s": 0.5, "autoanalyze_s": None}
    assert bench._analysed(linked) == "analysed"
    linked["statistics"]["tables"]["findings"]["autoanalyze_s"] = 0.2
    assert bench._analysed(linked) == (
        "analysed during the cycle end: findings; analysed: observations, results, run_keys"
    )
    assert bench._analysed(repetition() | {"statistics": None}) is None  # a file from before they were kept
    reps = [repetition("restarted", 1, run=20.0), repetition(run=10.0),
            repetition(n=3, run=30.0, analysed=None)]  # fmt: skip
    found = bench.summary(reps)
    assert set(found) == {"restarted", "warm, analysed", "warm, unanalysed"}  # never one median of 10 and 30
    medians = [found[f"warm, {kind}"]["run_s"]["median"] for kind in ("analysed", "unanalysed")]
    assert medians == [10, 30]
    made = result({100_000: reps})
    rendered = bench.render([(saved(tmp_path, "x", made), made)])
    assert "| warm, analysed | 10.0 |" in rendered and "| warm, unanalysed | 30.0 |" in rendered
    alike = result({100_000: [repetition("restarted", 1, analysed=None), repetition(analysed=None)]})
    assert alike["sizes"][0]["scenarios"][0]["summary"]["warm"]["statistics"] == "unanalysed"
    lines = bench.render([(saved(tmp_path, "y", alike), alike)]).splitlines()
    row = next(x for x in lines if "| warm |" in x)
    assert row.endswith("| unanalysed |")  # flagged beside the numbers


def test_fit_and_residual(tmp_path: Path) -> None:
    a, b, residual = bench.fit([1, 2, 3], [3, 5, 7])  # type: ignore[misc]
    assert (round(a, 9), round(b, 9), round(residual, 9)) == (1, 2, 0)  # type: ignore[arg-type]
    assert bench.fit([1, 2], [3, 5])[2] is None  # type: ignore[index]  # linearity not tested
    assert bench.fit([1], [3]) is None
    a, b, residual = bench.fit([1, 2, 3], [1, 4, 3])  # type: ignore[misc]
    assert (round(a, 6), round(b, 6), round(residual, 6)) == (round(2 / 3, 6), 1, round(2 / 3, 6))  # type: ignore[arg-type]
    two = result({10_000: [repetition()], 20_000: [repetition()]})
    rendered = bench.render([(saved(tmp_path, "x", two), two)])
    assert "(linearity not tested)" in rendered and "exponent between 10k and 20k: 0.00" in rendered


def modelled(rows: int, rss_per_key: float = 1e4) -> dict[str, Any]:
    """A warm repetition of dense at ``rows`` whose times follow the planning model exactly."""
    d = bench.truth("dense", rows)["candidates"]
    scan, first, cycle_end = 1 + 1e-5 * rows, 0.5 + 1e-6 * rows, 0.1 + 1e-4 * d
    run = max(scan, first + d / 1000) + cycle_end
    rss = 1e8 + rss_per_key * d
    rep = repetition(run=run, scan=scan, first=first, tail=d / 1000, cycle_end=cycle_end, rss=rss, rows=rows,
                     differing=d)  # fmt: skip
    rep["reads"]["tail_keys_per_s"] = 1000.0
    return rep


def test_extrapolate_labelled_and_capped(tmp_path: Path) -> None:
    measured = result({100_000: [modelled(100_000)], 1_000_000: [modelled(1_000_000)]})
    path = saved(tmp_path, "x", measured)
    files = [(path, measured)]
    plain = bench.render(files)
    assert "Extrapolated" not in plain and "T ≈" not in plain  # the model only under its heading
    extrapolated = bench.render(files, 10_000_000)
    heading = extrapolated.index("**Extrapolated, not measured**")
    assert "T ≈" not in extrapolated[:heading] and extrapolated.index(bench.CAVEAT) > heading
    # D at 10M is 100,000: cycle end 10.1 s and RSS 1.1 GB scale with it; the tail term leads the scan.
    assert "the model's error at 100k +0%, 1M +0%; at 10M: T ≈ 121 s beside peak RSS ≈ 1.1 GB" in extrapolated
    bigger = result({100_000: [modelled(100_000, 1e7)], 1_000_000: [modelled(1_000_000, 1e7)]})
    rendered = bench.render([(saved(tmp_path, "y", bigger), bigger)], 10_000_000)
    assert "at 10M: would not fit in 68.7 GB: not extrapolated" in rendered
    one = result({100_000: [modelled(100_000)]})
    assert "fewer than two sizes measured: not extrapolated" in bench.render([(path, one)], 1_000_000)
    assert report(path, "--extrapolate", "10M").exit_code == 0
    capped = report(path, "--extrapolate", "20M")
    assert (capped.exit_code, capped.stderr) == (
        2, f"error: --extrapolate 20M is more than 10 times the largest size measured in {path} (1M)\n"
    )  # fmt: skip
    failed = result({100_000: [modelled(100_000)], 1_000_000: [modelled(1_000_000)],
                     10_000_000: [repetition(rows=10**7, differing=100_000, correct=False)]})  # fmt: skip
    rendered = bench.render([(path, failed)], 100_000_000)  # the file's largest size is 10M, dense's 1M
    assert "`dense`: more than 10 times its largest measured size (1M): not extrapolated" in rendered


def test_noisy_sizes_are_marked_in_fits_and_the_model(tmp_path: Path) -> None:
    spread = [modelled(1_000_000), modelled(1_000_000)]
    spread[1]["seconds"]["run"] *= 1.5  # 1M's warm run_s spreads over 10% of its median
    made = result({100_000: [modelled(100_000)] * 2, 1_000_000: spread,
                   10_000_000: [modelled(10_000_000)] * 2})  # fmt: skip
    rendered = bench.render([(saved(tmp_path, "x", made), made)], 100_000_000)
    fits = next(line for line in rendered.splitlines() if line.startswith("- `bench-x.json` `dense`: run_s"))
    assert fits.count("; noisy at 1M)") == 5  # each fit uses 1M
    assert fits.endswith("run_s's exponent between 1M and 10M: none, noisy at 1M")
    errors = r"the model's error at 100k [+-]\d+%, 1M [+-]\d+% \(noisy\), 10M [+-]\d+%; at 100M: T ≈ [\d,]+ s"
    noisy = r" beside peak RSS ≈ \d+\.\d GB; its fits use noisy sizes \(1M\)$"
    assert re.search(errors + noisy, rendered, re.M)


def test_report_refuses_unpublishable(tmp_path: Path) -> None:
    other = tmp_path / "other.json"
    other.write_text('{"format": "something-else/1"}')
    for path in (other, tmp_path / "missing.json"):
        done = report(path)
        assert (done.exit_code, done.stderr) == (2, f"error: {path} is not a deltasleuth-bench/1 result\n")
    reps = [repetition("restarted", 1, power="battery"), repetition(correct=False)]
    path = saved(tmp_path, "draft", result({10_000: reps}, dirty=True))
    done = report(path)
    reasons = "uncommitted changes, a repetition not correct, on battery"
    assert (done.exit_code, done.stderr) == (
        2, f"error: {path} is not publishable ({reasons}): pass --draft to render it marked DRAFT\n"
    )  # fmt: skip
    drafted = report(path, "--draft")
    assert drafted.exit_code == 0
    assert drafted.stdout.startswith(f"**DRAFT: `{path.name}` is not publishable ({reasons}).**\n")
    assert "did not complete: MISMATCH 1, truth 0" in drafted.stdout  # failures stay in
    incomplete = result({10_000: [repetition("restarted", 1)]})
    incomplete["options"]["rows"].append(20_000)
    incomplete["complete"] = bench._complete(incomplete)
    assert "(incomplete)" in report(saved(tmp_path, "short", incomplete)).stderr
    slept = result({10_000: [repetition("restarted", 1, slept_s=6.0)]})
    unclean = result({10_000: [repetition("restarted", 1) | {"restart_clean": False}]})
    assert bench._unpublishable(slept) == ["the host slept"]
    assert bench._unpublishable(unclean) == ["an unclean restart"]
    assert bench._unpublishable(result({10_000: [repetition("restarted", 1)]})) == []


def test_unrun_sizes_and_scenarios_name_their_cause(tmp_path: Path) -> None:
    """Every requested size and scenario has a row; one that never ran, or ran fewer repetitions than asked,
    says why, and no fit takes its numbers."""
    made = result({100_000: [repetition("restarted", 1), repetition()]}, scenario="sparse")
    made["options"] |= {"rows": [100_000, 1_000_000], "scenarios": ["sparse", "dense", "clustered"]}
    partial = {"scenario": "dense", "truth": bench.truth("dense", 100_000),
               "repetitions": [repetition("restarted", 1)]}  # fmt: skip
    made["sizes"][0]["scenarios"].append(partial | {"summary": bench.summary(partial["repetitions"])})
    for cause in ("interrupted", "the benchmark database stopped (out of memory)", "failed (DiskFull)"):
        made["problems"] = [cause]
        lines = [x for x in bench.render([(saved(tmp_path, "x", made), made)]).splitlines() if "| `" in x]
        assert [x.split(" | ")[1:5] for x in lines] == [
            ["`sparse`", "100k", "2", "warm"], ["`dense`", "100k", "1,000", f"did not complete: {cause} |"],
            ["`clustered`", "100k", "1,000", f"did not complete: {cause} |"],
            *([f"`{s}`", "1M", f"{bench.truth(s, 10**6)['candidates']:,}", f"did not complete: {cause} |"]
              for s in ("sparse", "dense", "clustered")),
        ]  # fmt: skip
    assert bench._points(made, "dense") == []  # 1 of 2 repetitions
    made["sizes"][0]["scenarios"][1]["repetitions"] = [repetition("restarted", 1, correct=False)] * 2
    made["problems"] = []
    rows = bench.render([(saved(tmp_path, "y", made), made)])
    assert "| `dense` | 1M | 10,000 | did not complete: not run after a failure at a smaller size |" in rows
    assert "| `clustered` | 100k | 1,000 | did not complete: no cause recorded (the run was killed" in rows


def test_rows_are_marked_by_file(tmp_path: Path) -> None:
    """Two files' rows of one scenario and size stay apart, each naming its file, a draft's marked."""
    published = result({1_000_000: [repetition("restarted", 1), repetition(run=10.0)]})
    draft = result({1_000_000: [repetition("restarted", 1), repetition(run=5.0)]}, dirty=True)
    a, b = saved(tmp_path, "a", published), saved(tmp_path, "b", draft)
    rendered = bench.render([(a, published), (b, draft)])
    assert "| `bench-a.json` | `dense` | 1M | 10,000 | warm | 10.0 |" in rendered
    assert "| `bench-b.json` (DRAFT) | `dense` | 1M | 10,000 | warm | 5.00 |" in rendered


def test_first_is_shown_apart_under_server(tmp_path: Path) -> None:
    made = result({10_000: [repetition("first", 1, run=30.0, scan=20.0), repetition(run=10.0)]})
    rendered = bench.render([(saved(tmp_path, "x", made), made)])
    assert "restarted/first run s | restarted/first scan s |" in rendered and "| 30.0 | 20.0 |  |" in rendered


def test_conditions_say_stock_only_for_the_image_s_settings(tmp_path: Path) -> None:
    made = result({10_000: [repetition("restarted", 1)]})
    path = saved(tmp_path, "x", made)
    stock = bench._conditions(path, made)
    assert "observed on Mac15,9 (Apple M3 Max, 16 CPUs, 68.7 GB, darwin), synthetic data; Docker" in stock
    assert "PostgreSQL 16.14 (postgres:16-alpine) with stock settings holding" in stock
    settings = dict(bench.STOCK) | {"shared_buffers": "16GB", "work_mem": "256MB"}
    server = POSTGRES | {"image": None, "settings": settings}
    made["environment"] = ENVIRONMENT | {"topology": "remote", "docker": None, "postgres": server}
    tuned = bench._conditions(path, made)
    assert ("observed on the harness's host Mac15,9 (Apple M3 Max, 16 CPUs, 68.7 GB, darwin) (the database"
            " server's hardware is not recorded), synthetic data; a dedicated server; PostgreSQL 16.14 with"
            " non-stock settings (shared_buffers 16GB, work_mem 256MB) holding") in tuned  # fmt: skip
    assert "stock" not in bench.CAVEAT  # the caveat defers to the conditions line, for a tuned server too


def test_report_warns_changed_code_and_mixed_hosts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"
    scratch_repo(root)
    old = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
    ).stdout.strip()
    (root / "deltasleuth" / "x.py").write_text("changed\n")
    new = commit(root)
    monkeypatch.setattr(release, "ROOT", root)
    current = saved(tmp_path, "new", result({10_000: [repetition("restarted", 1)]}, sha=new))
    done = report(current)
    assert (done.exit_code, done.stderr) == (0, "")
    measured = saved(tmp_path, "old", result({10_000: [repetition("restarted", 1)]}, sha=old))
    elsewhere = saved(tmp_path, "else", result({20_000: [repetition("restarted", 1)]}, sha=new,
                                               host=HOST | {"model": "Mac16,1"}))  # fmt: skip
    done = report(current, measured, elsewhere)
    assert done.exit_code == 0
    assert done.stderr.splitlines() == [
        f"warning: {measured} was measured at {old[:12]}; deltasleuth/, tools/, pyproject.toml or uv.lock"
        " changed since",
        "warning: these files come from 2 hosts: their numbers are not one curve",
        "warning: these files come from 2 commits: their numbers are not one curve",
    ]
    (root / "docs" / "z.md").write_text("docs only\n")
    commit(root)
    assert report(current).stderr == ""  # a change outside the product's code


def test_caveat_block_verbatim(tmp_path: Path) -> None:
    """A report ends with what was not measured, then the caveat block, each word for word."""
    path = saved(tmp_path, "x", result({10_000: [repetition("restarted", 1)]}))
    assert report(path).stdout.endswith(f"\n{bench.NOT_MEASURED}\n\n{bench.CAVEAT}\n")


def test_rss_bytes_per_platform() -> None:
    assert bench._rss_bytes(123_456, "darwin") == 123_456  # darwin counts bytes
    assert bench._rss_bytes(123_456, "linux") == 123_456 * 1024  # Linux KiB


def test_exit_code_from_wait4_status() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(3)"])
    usage = bench._reap(proc, block=True)
    assert (proc.returncode, proc.poll(), usage.ru_maxrss > 0) == (
        3,
        3,
        True,
    )  # set at once: Popen waits no more
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    assert bench._reap(sleeper) is None and sleeper.returncode is None
    os.kill(sleeper.pid, signal.SIGKILL)
    bench._reap(sleeper, block=True)
    assert sleeper.returncode == -signal.SIGKILL


def test_env_scrubbed(stubs: Stubs, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGPASSWORD", "hunter2")
    monkeypatch.setenv("PGHOST", "db.example")
    monkeypatch.setenv("DS_CONFIG", "/etc/deltasleuth.yaml")
    monkeypatch.setenv("DOCKER_CONTEXT", "callers")
    monkeypatch.setenv("DOCKER_HOST", "unix:///callers.sock")
    stubs.scenario(df=[1])  # refused at stage 3, after the container
    before = dict(os.environ)
    done = stubs.bench("--allow-dirty")
    assert done.exit_code == 2 and dict(os.environ) == before
    runs = stubs.runs()
    assert runs[0]["argv"][:2] == ["context", "inspect"]  # resolved once, with the caller's Docker settings
    assert (runs[0]["env"]["DOCKER_CONTEXT"], runs[0]["env"]["DOCKER_HOST"]) == (
        "callers",
        "unix:///callers.sock",
    )
    assert len(runs) > 3
    for run in runs:
        assert not [name for name in run["env"] if name.startswith(("PG", "DS_"))]
    for run in runs[1:]:
        assert "DOCKER_CONTEXT" not in run["env"] and run["env"]["DOCKER_HOST"] == SCENARIO["endpoint"]
    env = bench._env(SCENARIO["endpoint"])
    fake = SimpleNamespace(work=stubs.tmp, env=env, opts=SimpleNamespace(ds=stubs.bin / "ds"))
    bench._ds(fake, "init", "--config", "x")  # type: ignore[arg-type]
    [ds] = stubs.runs("ds")
    added = ("__CF_USER_TEXT_ENCODING", "LC_CTYPE")
    assert ds["argv"] == ["init", "--config", "x"]
    assert ds["env"] == {name: value for name, value in env.items() if name not in added}


def test_dirty_includes_untracked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"
    scratch_repo(root)
    monkeypatch.setattr(release, "ROOT", root)
    assert bench._dirty(bench._env()) == []
    (root / "docs" / "x.md").write_text("not the product\n")
    assert bench._dirty(bench._env()) == []
    (root / "tools" / "x.py").write_text("new\n")
    assert bench._dirty(bench._env()) == ["?? tools/x.py"]


def test_container_argv(stubs: Stubs) -> None:
    stubs.scenario(df=[1])
    done = stubs.bench("--allow-dirty")
    assert done.exit_code == 2
    [run] = [r["argv"] for r in stubs.runs() if r["argv"][0] == "run"]
    name, label = run[run.index("--name") + 1], run[run.index("--label") + 1]
    assert re.fullmatch(r"dsbench-[0-9a-f]{8}", name) and label == f"deltasleuth.bench={name[8:]}"
    assert run[:4] == ["run", "-d", "--pull", "never"] and run[-1] == "postgres:16-alpine"
    assert re.fullmatch(r"127\.0\.0\.1:\d+:5432", run[run.index("-p") + 1])  # loopback only
    assert run[run.index("--shm-size") + 1] == "1g"
    assert run[run.index("-e") + 1] == "POSTGRES_PASSWORD_FILE=/run/secrets/pg_password"
    assert (
        run[run.index("-v") + 1]
        == f"{stubs.work.resolve()}/dsbench-{name[8:]}/pg_password:/run/secrets/pg_password:ro"
    )
    password = stubs.state()["password"]
    assert len(password) >= 32
    container = bench.Container(name[8:], stubs.work, bench._env(SCENARIO["endpoint"]))
    stubs.write_state({"containers": [name], "password": password})
    starts = iter([1, 2])
    assert container.restart(lambda: next(starts)) is True  # type: ignore[arg-type,return-value]
    assert [r["argv"] for r in stubs.runs()][-3][:4] == ["restart", "-t", "300", name]
    assert all(password not in json.dumps(r) for r in stubs.runs())  # no argv or environment holds it
    assert re.search(r"dbname=postgres", container.dsn()) and "sslmode=disable" in container.dsn()
    assert conninfo_to_dict(container.dsn())["hostaddr"] == "127.0.0.1"  # never PGHOSTADDR's


def test_unclean_restart_is_a_problem(stubs: Stubs) -> None:
    container = bench.Container("0123abcd", stubs.work, bench._env(SCENARIO["endpoint"]))
    stubs.write_state({"containers": [container.name], "password": None})
    starts = itertools.count()
    assert container.restart(lambda: next(starts)) is True  # type: ignore[arg-type,return-value]
    assert container.restart(lambda: 5) is False  # type: ignore[arg-type,return-value]  # it did not restart
    stubs.scenario(logs="LOG:  database system was not properly shut down; automatic recovery in progress\n")
    assert container.restart(lambda: next(starts)) is False  # type: ignore[arg-type,return-value]
    unclean = result({10_000: [repetition("restarted", 1) | {"restart_clean": False}]})
    assert bench._unpublishable(unclean) == ["an unclean restart"]


def test_a_sleep_is_a_problem() -> None:
    rep = repetition(slept_s=6.0)
    expected = {k: rep["observed"][k] for k in ("rows_a", "rows_b", "candidates")}
    assert bench._judge(rep, expected, {}) == ["host slept 6.0 s"]
    assert bench._judge(repetition(slept_s=4.9), expected, {}) == []


def test_remote_docker_endpoint_refused(stubs: Stubs) -> None:
    stubs.scenario(endpoint="tcp://10.0.0.5:2376")
    done = stubs.bench("--allow-dirty")
    assert (done.exit_code, done.stderr) == (2, (
        "error: the Docker endpoint is not a local socket (tcp://): the port and the password file would be"
        " on another machine; use a local Docker, or --server file:<absolute path>\n"
    ))  # fmt: skip
    assert [r["argv"][:2] for r in stubs.runs()] == [["context", "inspect"]]
    stubs.scenario(endpoint="npipe:////./pipe/docker_engine")
    assert bench._endpoint() == "npipe:////./pipe/docker_engine"


def test_server_dsn_keeps_tls(tmp_path: Path, stubs: Stubs) -> None:
    admin = "postgresql://admin:s3cret@db.bank.example:5432/postgres?sslmode=verify-full&sslrootcert=/ca.pem"
    server = bench.Server(admin, "0123abcd")
    assert server.topology == "remote" and server.dsn() == admin
    assert conninfo_to_dict(server.dsn("dsbench_0123abcd_a")) == conninfo_to_dict(admin) | {
        "dbname": "dsbench_0123abcd_a"
    }
    (tmp_path / "admin").write_text("host=/var/run/postgresql user=postgres\n")
    assert bench._admin(f"file:{tmp_path / 'admin'}") == "host=/var/run/postgresql user=postgres"  # a socket
    assert bench.Server("host=127.0.0.1 user=postgres", "0123abcd").topology == "loopback"
    for dsn, mode in (("host=db.bank.example user=postgres sslmode=require", "require"),
                      ("postgresql://postgres@10.1.2.3/postgres", "prefer"),
                      ("host=localhost,db.bank.example sslmode=verify-ca", "verify-ca")):  # fmt: skip
        (tmp_path / "admin").write_text(f"{dsn}\n")
        done = stubs.bench("--server", f"file:{tmp_path / 'admin'}")
        assert (done.exit_code, done.stderr) == (2, (
            f"error: the server's DSN names a remote host with sslmode {mode}: use sslmode=verify-full, since"
            " the benchmark connects as a superuser\n"
        ))  # fmt: skip
    assert stubs.runs() == [] and stubs.left() == []


def test_server_dsn_is_self_contained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """libpq fills what an admin DSN omits from the caller's PG* environment for the harness's own
    connections, which the children's scrubbed environment never sees: no hostaddr may come from it."""
    admin = tmp_path / "admin"
    admin.write_text("host=127.0.0.1 user=postgres\n")
    for name in ("PGHOSTADDR", "PGSERVICE"):
        with monkeypatch.context() as patch:
            patch.setenv(name, "192.0.2.1")
            with pytest.raises(bench.typer.Exit):
                bench._admin(f"file:{admin}")
            admin.write_text("host=127.0.0.1 hostaddr=127.0.0.1 user=postgres\n")
            assert bench._admin(f"file:{admin}") == "host=127.0.0.1 hostaddr=127.0.0.1 user=postgres"
            admin.write_text("host=127.0.0.1 user=postgres\n")
    assert bench._admin(f"file:{admin}") == "host=127.0.0.1 user=postgres"
    given = {"PGPORT": "port=5432", "PGUSER": "user=postgres", "PGPASSWORD": "password=x",
             "PGSSLMODE": "sslmode=disable"}  # fmt: skip
    admin.write_text("host=127.0.0.1\n")
    for name, key in given.items():
        with monkeypatch.context() as patch:
            patch.setenv(name, "1")
            with pytest.raises(bench.typer.Exit):  # a port, user, password or TLS mode from the environment
                bench._admin(f"file:{admin}")
            admin.write_text(f"host=127.0.0.1 {key}\n")
            assert bench._admin(f"file:{admin}") == f"host=127.0.0.1 {key}"
            admin.write_text("host=127.0.0.1\n")
    admin.write_text("host=127.0.0.1 user=postgres\n")
    monkeypatch.setenv("PGHOSTADDR", "192.0.2.1")
    done = CliRunner().invoke(bench.app, ["run", "--out", str(tmp_path), "--server", f"file:{admin}"])
    assert (done.exit_code, done.stderr) == (2, (
        "error: --server: PGHOSTADDR is set and the admin DSN names no hostaddr: the harness's own"
        " connections would take it; unset PGHOSTADDR, or give hostaddr=\n"
    ))  # fmt: skip


def test_server_guard_messages(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    listed = [("postgres", None), ("template0", None), ("template1", None),
              ("dsbench_0123abcd_a", "deltasleuth-bench 0123abcd")]  # fmt: skip

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *exc: object) -> None:
            pass

        def execute(self, query: str) -> Any:
            return SimpleNamespace(fetchone=lambda: (True,), fetchall=lambda: listed)

    def unreachable(dsn: str, attempts: int = 60) -> Any:
        raise psycopg.OperationalError("connection refused")

    for connect, message in (
        (lambda dsn, attempts=60: Connection(), "1 benchmark databases exist: another run is under way or was"
         " killed; if none is running, run: uv run python tools/bench.py clean --server file:/srv/admin"),
        (unreachable, "the server is unreachable (OperationalError): check --server's DSN"),
    ):  # fmt: skip
        monkeypatch.setattr(bench, "_connect", connect)
        with pytest.raises(bench.typer.Exit):
            bench._server_guard("file:/srv/admin", "host=127.0.0.1 user=postgres")
        assert capsys.readouterr().err == f"error: {message}\n"


def _setup(monkeypatch: pytest.MonkeyPatch, environment: Any = None) -> None:
    """The run past stage 3 without a database: the environment and the store's setup replaced."""
    monkeypatch.setattr(bench, "environment", environment or (lambda *_: ENVIRONMENT))
    monkeypatch.setattr(bench, "_prepare", lambda _: 10**8)


def test_teardown_when_generation_fails(stubs: Stubs, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup(monkeypatch)

    def refused(*_: Any) -> None:
        raise psycopg.errors.DiskFull("could not extend file")

    monkeypatch.setattr(bench, "generate", refused)
    done = stubs.bench("--allow-dirty")
    assert isinstance(done.exception, psycopg.errors.DiskFull)
    [name] = [r["argv"][r["argv"].index("--name") + 1] for r in stubs.runs() if r["argv"][0] == "run"]
    assert ["rm", "-f", "-v", name] in [r["argv"] for r in stubs.runs()] and stubs.state()["containers"] == []
    assert list(stubs.work.iterdir()) == []  # the work folder, password and DSN files with it
    [path] = stubs.out.iterdir()
    assert json.loads(path.read_text())["problems"] == ["failed (DiskFull)"]  # named in the file


def test_teardown_when_a_second_signal_cuts_the_removal_short(
    stubs: Stubs, monkeypatch: pytest.MonkeyPatch
) -> None:
    remove, before = bench.Container.remove, signal.getsignal(signal.SIGTERM)

    def cut(self: bench.Container) -> None:
        remove(self)
        raise KeyboardInterrupt  # a second Ctrl-C while docker rm runs

    monkeypatch.setattr(bench.Container, "remove", cut)
    stubs.scenario(df=[1])  # refused at stage 3
    done = stubs.bench("--allow-dirty")
    assert done.exit_code == 130 and stubs.state()["containers"] == []  # interrupted
    assert list(stubs.work.iterdir()) == [] and signal.getsignal(signal.SIGTERM) == before


def test_sighup_unwinds_as_ctrl_c(stubs: Stubs, monkeypatch: pytest.MonkeyPatch) -> None:
    def callers(*_: Any) -> None:
        raise AssertionError("SIGHUP reached the caller's handler")

    previous = signal.signal(signal.SIGHUP, callers)
    try:
        monkeypatch.setattr(bench, "_preflight", lambda _: os.kill(os.getpid(), signal.SIGHUP))
        done = stubs.bench("--allow-dirty")
        assert (done.exit_code, done.stderr) == (130, "interrupted\n")  # a closed terminal or SSH session
        assert stubs.state()["containers"] == [] and list(stubs.work.iterdir()) == []
        assert signal.getsignal(signal.SIGHUP) is callers  # given back
        signal.signal(signal.SIGHUP, signal.SIG_IGN)  # nohup: a hangup stays ignored, and the run goes on
        seen = []
        monkeypatch.setattr(bench, "_preflight", lambda _: seen.append(signal.getsignal(signal.SIGHUP)))
        stubs.bench("--allow-dirty")
        assert seen == [signal.SIG_IGN] and signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, previous)


def fake_size(store_bytes: int) -> Any:
    """A size's work: its tables and one correct repetition, the store grown by ``store_bytes``."""

    def size(b: bench.Bench, rows: int, entry: dict[str, Any]) -> None:
        sizes = {"table_bytes": 70 * rows, "index_bytes": 30 * rows, "row_bytes": 70, "generated_s": 0.1}
        entry["tables"]["users.a"] = {"rows": rows, "columns": 4} | sizes
        rep = repetition("restarted", 1, rows=rows, differing=bench.truth("sparse", rows)["candidates"])
        rep["stored"]["store_bytes"] = store_bytes
        record = {"scenario": "sparse", "truth": bench.truth("sparse", rows), "repetitions": [rep]}
        entry["scenarios"].append(record | {"summary": bench.summary([rep])})
        entry["fits_in_memory"] = True
        bench._write(b)

    return size


def test_refusals_by_stage(stubs: Stubs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    (home / "Desktop" / "benches").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    (tmp_path / "remote").write_text("host=db.bank.example user=postgres\n")
    (tmp_path / "garbled").write_text("host='db.bank.example password=unterminated\n")
    (tmp_path / "hostless").write_text("user=postgres password=x dbname=postgres\n")  # PGHOST would decide
    (tmp_path / "service").write_text("service=bench host=127.0.0.1\n")
    rows = "sizes are multiples of 10,000 from 10,000 to 1,000,000,000, each once"
    stage1 = {
        ("--work", str(tmp_path / "nowhere")): f"--work {tmp_path / 'nowhere'} is not a folder",
        ("--work", str(release.ROOT / "docs")): f"--work {release.ROOT / 'docs'} is in a git working tree:"
        " evidence would sit beside the code",
        ("--work", str(home / "Desktop" / "benches")): f"--work {home / 'Desktop' / 'benches'} is in a"
        " cloud-synced folder (~/Desktop): use a local folder",
        ("--rows", "25k"): f"--rows 25k: {rows}",
        ("--scenarios", "sparse,soak"): "--scenarios soak: choose from sparse, dense, clustered, graph",
        ("--shape", "tall"): "--shape must be narrow or wide",
        ("--server", "admin-dsn"): "--server must be file:<absolute path>",
        ("--server", f"file:{tmp_path / 'garbled'}"): "--server: the admin DSN is not a valid connection"
        " string",
        ("--server", f"file:{tmp_path / 'missing'}"): "--server: cannot read secret file"
        f" {tmp_path / 'missing'}: No such file or directory",
        ("--server", f"file:{tmp_path / 'hostless'}"): "--server: the admin DSN names no host: give host= or"
        " hostaddr= (a socket folder counts), or libpq takes PGHOST",
        ("--server", f"file:{tmp_path / 'service'}"): "--server: the admin DSN names a service: write its"
        " parameters in the file",
        ("--out", str(tmp_path / "none")): f"--out {tmp_path / 'none'} is not a folder",
        ("--server", f"file:{tmp_path / 'remote'}"): "the server's DSN names a remote host with sslmode"
        " prefer: use sslmode=verify-full, since the benchmark connects as a superuser",
    }  # fmt: skip
    for args, message in stage1.items():
        done = stubs.bench(*args)
        assert (done.exit_code, done.stderr) == (2, f"error: {message}\n"), args
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python"))
    done = stubs.bench()
    assert done.stderr == f"error: ds is not installed next to {tmp_path / 'python'}: run uv sync\n"
    monkeypatch.setattr(sys, "executable", str(stubs.bin / "python"))
    assert stubs.runs() == [] and stubs.left() == []  # stage 1 runs nothing and makes nothing
    root = tmp_path / "repo"
    scratch_repo(root)
    (root / "uv.lock").write_text("changed\n")
    monkeypatch.setattr(release, "ROOT", root)
    done = stubs.bench()
    assert done.stderr == (
        "error: deltasleuth/, tools/, pyproject.toml or uv.lock has uncommitted or untracked changes: commit"
        " them, or pass --allow-dirty (the result is then not publishable)\n"
    )
    stubs.scenario(existing=["dsbench-cafebabe"])
    assert stubs.bench("--allow-dirty").stderr == (
        "error: a benchmark container (dsbench-cafebabe) exists: another run is under way or was killed; if"
        " none is running, run: uv run python tools/bench.py clean\n"
    )
    stubs.scenario(existing=[], image="")
    assert stubs.bench("--allow-dirty").stderr == (
        "error: postgres:16-alpine (the benchmark's database) is not in the local image store; this tool"
        " never pulls. To add it: docker pull postgres:16-alpine\n"
    )
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert stubs.bench("--allow-dirty").stderr == (
        "error: docker is not available: install Docker, or give --server file:<absolute path>\n"
    )
    assert "run" not in [r["argv"][0] for r in stubs.runs()] and stubs.left() == []  # stage 2 starts nothing
    monkeypatch.setenv("PATH", f"{stubs.bin}{os.pathsep}/usr/bin:/bin:/usr/sbin:/sbin")
    monkeypatch.setattr(release, "ROOT", Path(bench.__file__).resolve().parents[1])
    stubs.scenario(image=SCENARIO["image"], df=[10**6])  # 1 GB: 10k fits, 10M does not
    done = stubs.bench("--allow-dirty", rows="10k,10M")
    assert done.exit_code == 2
    need = (r"error: 10M rows a side need about \d+\.\d GB free on the database volume \(it has 1\.0 GB\)"
            r" and \d+\.\d GB in the work folder \(it has \d+\.\d GB(; on darwin it holds both)?\): free"
            r" space, or run fewer sizes\n")  # fmt: skip
    assert re.fullmatch(need, done.stderr)
    assert stubs.state()["containers"] == [] and stubs.left() == []  # the container removed, no file
    _setup(monkeypatch)
    monkeypatch.setattr(bench, "_size", fake_size(10**15))  # 10k measures a huge store per differing key
    stubs.scenario(df=[97_656_250])  # 100 GB
    done = stubs.bench("--allow-dirty", "--scenarios", "sparse", rows="10k,20k")
    assert done.exit_code == 1, done.output
    assert re.search(r"^did not run: 20k rows need about \d+\.\d GB free, 100\.0 GB left$", done.stderr, re.M)
    [path] = stubs.out.iterdir()
    kept = json.loads(path.read_text())
    assert (kept["complete"], kept["publishable"], kept["sizes"][0]["did_not_run"]) == (False, False, None)
    assert kept["sizes"][1]["did_not_run"].startswith("did not run: 20k rows need about")
    assert stubs.state()["containers"] == [] and list(stubs.work.iterdir()) == []


@pytest.mark.parametrize(
    ("raised", "code", "cause"),
    [(KeyboardInterrupt(), 130, "interrupted"),
     (bench._Stopped("the benchmark database stopped (out of memory)"), 1,
      "the benchmark database stopped (out of memory)"),
     (psycopg.errors.DiskFull("could not extend file"), 1, "failed (DiskFull)")],
)  # fmt: skip
def test_a_stopped_run_keeps_its_file_and_names_what_did_not_run(
    stubs: Stubs, monkeypatch: pytest.MonkeyPatch, raised: BaseException, code: int, cause: str
) -> None:
    """After Ctrl-C, a stopped database or a generation error the file names the cause, and report gives
    each requested size and scenario that did not run."""
    _setup(monkeypatch)
    sized = fake_size(10**6)  # sparse only

    def size(b: bench.Bench, rows: int, entry: dict[str, Any]) -> None:
        if rows > 10_000:
            raise raised
        sized(b, rows, entry)

    monkeypatch.setattr(bench, "_size", size)
    done = stubs.bench("--allow-dirty", "--scenarios", "sparse,dense", rows="10k,20k")
    assert done.exit_code == code, done.output
    assert ("interrupted" in done.stderr) is (code == 130)
    [path] = stubs.out.iterdir()
    kept = json.loads(path.read_text())
    assert (kept["problems"], kept["complete"], kept["publishable"]) == ([cause], False, False)
    drafted = report(path, "--draft").stdout
    for scenario, rows, d in (("dense", "10k", "100"), ("sparse", "20k", "1"), ("dense", "20k", "200")):
        assert f"| `{scenario}` | {rows} | {d} | did not complete: {cause} |" in drafted
    assert stubs.state()["containers"] == [] and list(stubs.work.iterdir()) == []


def test_space_counts_docker_desktop_s_disk_on_darwin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """10M dense narrow needs about 6.5 GB on the database volume and 1.8 GB of evidence."""
    data, work = tmp_path / "data", tmp_path / "work"
    free = {data: 10**9, work: 10**12}
    monkeypatch.setattr(bench.sys, "platform", "darwin")
    monkeypatch.setattr(bench, "_docker_data", lambda: data)
    monkeypatch.setattr(bench.shutil, "disk_usage", lambda path: SimpleNamespace(free=free[path]))
    fake = SimpleNamespace(db=SimpleNamespace(free=lambda: 10**12), work=work,
                           opts=SimpleNamespace(scenarios=["dense"], shape="narrow"))  # fmt: skip
    monkeypatch.setattr(bench, "_same_disk", lambda a, b: False)  # the work folder on another disk
    assert bench._space(fake, 10_000_000, bench.ESTIMATES) == (  # type: ignore[arg-type]
        "10M rows a side need about 6.5 GB free on the database volume (it has 1000.0 GB), 6.5 GB on the disk"
        " holding Docker Desktop's data (it has 1.0 GB) and 1.8 GB in the work folder (it has 1000.0 GB):"
        " free space, or run fewer sizes", "did not run: 10M rows need about 6.5 GB free, 1.0 GB left",
    )  # fmt: skip
    monkeypatch.setattr(bench, "_same_disk", lambda a, b: True)  # one disk holds both
    free |= {data: 10**12, work: 8 * 10**9}
    assert bench._space(fake, 10_000_000, bench.ESTIMATES)[0] == (  # type: ignore[arg-type,index]
        "10M rows a side need about 6.5 GB free on the database volume (it has 1000.0 GB) and 8.2 GB in the"
        " work folder (it has 8.0 GB; on darwin it holds both): free space, or run fewer sizes"
    )
    fake.db.free = lambda: None  # --server: its volume is not checked
    free[work] = 10**9
    assert bench._space(fake, 10_000_000, bench.ESTIMATES)[0] == (  # type: ignore[arg-type,index]
        "10M rows a side need about 1.8 GB in the work folder (it has 1.0 GB): free space, or run fewer sizes"
    )


def _watch(stubs: Stubs, polls: Any, *, step: float = 1.0, limits: bench.Limits | None = None,
           stopped: Any = lambda: "unreachable", **scenario: Any) -> bench.Watched:  # fmt: skip
    """``bench._watch`` of the stub ds following ``scenario``: scripted polls, a clock ``step`` apart."""
    stubs.scenario(**({"run": [], "sleep": 20, "exit": 0} | scenario))
    err = stubs.tmp / "ds-run.err"
    with err.open("wb") as stderr:
        proc = subprocess.Popen([str(stubs.bin / "ds"), "run"], stderr=stderr)
    time.sleep(0.5)  # its lines written
    clock = itertools.count(0, step)
    limits = limits or bench.Limits(240, 10, 10**12)
    return bench._watch(
        proc, polls, lambda: next(clock), err=err, limits=limits, stopped=stopped, env=bench._env()
    )


def running(count: bool = False) -> dict[str, Any]:
    opened = 5 if count else True
    return {
        "status": "running",
        "done": False,
        "checkpoint": 0,
        "open": opened,
        "source": False,
        "elapsed": 1.0,
    }


def test_watchdog_aborts(stubs: Stubs) -> None:
    started = time.monotonic()
    assert _watch(stubs, running, step=30, limits=bench.Limits(1, 10, 10**12)).abort == "timeout after 1 min"
    assert (
        _watch(stubs, running, limits=bench.Limits(240, 10, 1)).abort
        == "peak memory 0.0 GB over --max-rss 0.0 GB"
    )
    line = "run R-01ABCDEFGHJKMNPQRSTVWXYZ cannot end its cycle: ValueError"
    stalled = _watch(stubs, running, step=100, limits=bench.Limits(240, 1, 10**12), run=[line])
    assert stalled.abort == f"stalled: no progress for 1 min ({line})"
    sorting = [running() | {"source": True}] * 3 + [
        running()
    ] * 10  # a source sorting is progress until t_done
    ticks = iter(sorting)
    busy = _watch(stubs, lambda count=False: next(ticks), step=50, limits=bench.Limits(240, 1, 10**12))
    assert busy.abort is not None and busy.abort.startswith("stalled: no progress for 1 min")
    assert len(list(ticks)) == 8  # five polls: three with the source busy, two without
    paused = _watch(
        stubs, running, run=["scan of run R-01ABCDEFGHJKMNPQRSTVWXYZ paused: ConfigurationLimitExceeded"]
    )
    assert paused.abort == "scan paused: ConfigurationLimitExceeded"
    saved_ = "keys of run R-01ABCDEFGHJKMNPQRSTVWXYZ not saved: OperationalError"
    assert _watch(stubs, running, run=[saved_] * 3).abort == f"{saved_} (3 times)"
    twice = _watch(stubs, running, run=[saved_] * 2 + ["worker slot: ValueError"] * 3, sleep=0, exit=0)
    assert twice.abort is None  # twice, then exited 0 on its own
    assert _watch(stubs, running, sleep=0, exit=2).abort == "ds exited 2"
    assert _watch(stubs, running, sleep=0, signal=signal.SIGTERM).abort == "killed by signal 15"
    stubs.write_state({"containers": ["dsbench-0123abcd"], "password": None})
    container = bench.Container("0123abcd", stubs.work, bench._env(SCENARIO["endpoint"]))

    def lost(count: bool = False) -> dict[str, Any]:
        raise psycopg.OperationalError("server closed the connection unexpectedly")

    for state, cause in (("true false 137", "out of memory"), ("false false 1", "exited 1"),
                         ("false true 0", "unreachable")):  # fmt: skip
        stubs.scenario(state=state)
        found = _watch(stubs, lost, stopped=container.stopped)
        assert found.abort == f"the benchmark database stopped ({cause})"
    assert time.monotonic() - started < 60


def test_watch_times_the_phases(stubs: Stubs) -> None:
    done = running() | {"checkpoint": 2, "done": True}
    drained = done | {"open": False}
    rows = [None, running() | {"temp": 10}, running() | {"checkpoint": 1, "temp": 90}, done,
            done | {"open": 7}, done | {"temp": 50}, drained,
            drained | {"status": "completed", "elapsed": 9.5}]  # fmt: skip
    polls = iter(rows)

    def poll(count: bool = False) -> dict[str, Any] | None:
        row = next(polls, rows[-1])
        return row if row is None or not count else row | {"open": 7 if row["open"] else 0}

    w = _watch(stubs, poll, sleep=2)
    assert w.abort is None and w.usage is not None and w.t["reaped"] > 7
    # t_done's one count takes the next row: open keys 7 at the scan's end, none two polls later.
    assert (w.t["progress"], w.t["done"], w.open_at_done, w.t["drained"], w.t["end"], w.run_s) == (
        3,
        4,
        7,
        6,
        7,
        9.5,
    )
    assert w.temp_peak == 90  # the most temp file bytes the polls saw held at once


def test_measured_takes_the_temp_held_at_once() -> None:
    """graph writes L1's and then L2's sorts: the re-check counts the most held at once, not all written."""
    rep = repetition(rows=100_000, differing=20)
    for side in "ab":
        rep["sources"][side]["temp_bytes"] = 21_000_000  # 210 B a fact a side written
    rep["temp_peak_bytes"] = 24_000_000  # held at once: both sides' L1 sorts
    table = {"rows": 100_000, "columns": 3, "table_bytes": 10**7, "index_bytes": 5 * 10**6, "row_bytes": 41}
    entry = {"tables": {"links.a": table}, "scenarios": [{"scenario": "graph", "repetitions": [rep]}]}
    fake = SimpleNamespace(constants=dict(bench.ESTIMATES), opts=SimpleNamespace(shape="narrow"))
    bench._measured(fake, entry)  # type: ignore[arg-type]
    assert (fake.constants["temp.links"], fake.constants["links"]) == (120, 150)


def test_result_holds_no_secret_or_host(stubs: Stubs, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup(monkeypatch, lambda b, image: ENVIRONMENT | {"host": bench._host(b.env), "docker": b.db.docker()})
    monkeypatch.setattr(bench, "_size", fake_size(10**6))
    done = stubs.bench("--allow-dirty", "--scenarios", "sparse")
    assert done.exit_code == 0, done.output
    [path] = stubs.out.iterdir()
    text, kept = path.read_text(), json.loads(path.read_text())
    assert kept["options"]["server"] is False and kept["complete"] is True
    secrets = [stubs.state()["password"], socket.gethostname(), getpass.getuser(), str(Path.home()),
               str(stubs.tmp), str(stubs.tmp.resolve()), "file:/", "password=", "host=",
               "docker-desktop"]  # fmt: skip
    assert [s for s in secrets if s in text] == []
    assert kept["profile"]["settings"]["database_dsn_ref"] == "file:<work>/dsn_run"
    assert (
        kept["profile"]["specs"]["bench-tables"]["sha256"]
        == hashlib.sha256(
            json.dumps(kept["profile"]["specs"]["bench-tables"]["document"], indent=1).encode()
        ).hexdigest()
    )


def test_clean_removes_marked_work_folders(stubs: Stubs) -> None:
    for name, marked in (("dsbench-0123abcd", True), ("dsbench-cafebabe", False), ("elsewhere", True)):
        (stubs.work / name).mkdir()
        if marked:
            (stubs.work / name / ".deltasleuth-bench").write_text("deltasleuth-bench x\n")
    stubs.scenario(existing=["c0ffee01", "c0ffee02"])
    done = CliRunner().invoke(bench.app, ["clean", "--work", str(stubs.work)])
    assert (done.exit_code, done.stdout) == (
        0,
        "removed 2 container(s), 0 database(s) and 1 work folder(s)\n",
    )
    assert sorted(p.name for p in stubs.work.iterdir()) == ["dsbench-cafebabe", "elsewhere"]
    assert ["rm", "-f", "-v", "c0ffee01", "c0ffee02"] in [r["argv"] for r in stubs.runs()]
    assert ["ps", "-aq", "--filter", "label=deltasleuth.bench"] in [r["argv"] for r in stubs.runs()]
