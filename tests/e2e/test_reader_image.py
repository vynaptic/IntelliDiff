"""The file reader in the image (docs/mvp/READER.md §3: the image tests of U32 to U37), on Linux under the
compose hardening. ``DS_IMAGE=<image>`` runs them; nothing is pulled and nothing installed.

The image holds no pytest and no tests: each test is one ``docker run --rm --pull never --network none`` of
the image with ``release.HARDENED``, a settings directory and an evidence volume as the release's smoke step
mounts them, and a volume at ``/var/lib/deltasleuth/files`` for the files it writes. It runs one ``python -c``
text against the product's own ``duck`` and ``reader`` and prints one line, which is asserted. An image of
another machine than the host's runs under emulation, where a data limit counts the emulator (a reader
with one ends as it starts) and the emulator loads no socket filter: its readers then start with
``files.os_limits`` off, and the test of the default limit is skipped. Emulation is no kernel of that
machine: what an x86_64 kernel does is not observed here.
"""

from __future__ import annotations

import base64
import os
import re
import secrets
import subprocess
from collections.abc import Callable, Iterator
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.support import crafted
from tools import release

IMAGE = os.environ.get("DS_IMAGE", "")
pytestmark = [pytest.mark.e2e, pytest.mark.skipif(not IMAGE, reason="DS_IMAGE=<image> runs it")]

FILES = "/var/lib/deltasleuth/files"
SETTINGS = (
    "database_dsn_ref: file:/etc/deltasleuth/db_dsn\ntargets_file: targets.yaml\n"
    "evidence_dir: /var/lib/deltasleuth/evidence\nworkspace_dir: /var/lib/deltasleuth/workspace\n"
)
TARGETS = {
    "exports": {"engine": "files", "root": f"{FILES}/root", "settle_s": 0, "allow_writable": True},
    "svc": {"base_url": "https://svc.example", "capabilities": {"authoritative_absence": {"status": [404]}}},
}
# What every text starts with: a reader as the product starts it, with its limits where the image runs
# natively (``LIMITS``, set by the harness), reading the files volume's ``root``.
PRELUDE = """
import asyncio, base64, os, re, signal, subprocess, sys, threading, time
from pathlib import Path
from deltasleuth.config import load_settings
from deltasleuth.connectors import tables
from deltasleuth.connectors.tables import duck, manifest, reader
from deltasleuth.core.spec import compile_spec, load_targets
settings = load_settings()
root, probe = Path("/var/lib/deltasleuth/files/root"), Path("/var/lib/deltasleuth/files/outside.csv")
root.mkdir(exist_ok=True)
probe.write_bytes(duck.CSV)
(root / "two.parquet").write_bytes(duck.PARQUET)
def start(memory=256, limit=None):
    limit = (limit or duck._limit(memory, 2)) if LIMITS else None
    return duck._spawned(settings.spill_dir, [str(root)], probe, memory, 1024, 2, limit)
def status(pid, name):
    return int(re.search(name + r":\\s+([0-9]+)", Path(f"/proc/{pid}/status").read_text())[1])
"""
# A Parquet side planned as a run's creation plans it, and its scan, through the files engine.
PLANNED = """
found = load_targets(settings.targets_file)
duck.configure(settings, found)
engine, exports = tables.ENGINES["files"], found.get("exports")
def side(role, path, key):
    file = {"path": path, "format": "parquet"}
    return {"role": role, "target": "exports", "table": {"name": "t", "keyColumns": [key], "file": file}}
async def planned(a, b, key):
    spec = {
        "apiVersion": "deltasleuth/v1", "kind": "ComparisonSpec",
        "metadata": {"name": "files", "version": "1.0.0", "owner": "tests"}, "normalizerVersion": "1.1.0",
        "key": {"name": "id", "type": "string", "class": "business_code", "pattern": "^.+$",
                "source": {"kind": "list", "keys": ["1"]}},
        "sides": {"a": side("legacy", a, key), "b": side("new", b, key)},
        "compare": {}, "recheck": {"delay": "0s", "times": 1}, "security": {"unlisted": "clear"},
    }
    made = await tables.planning(found, compile_spec(spec, targets=found))
    return tables.pins(made.plan, made.pinned)
async def scanned(plan):
    async with engine.snapshot(exports) as session:
        return sum([len(batch) async for batch in await engine.scan(session, plan, None)])
"""
# FILES §17's people written by an unrestricted DuckDB, then a pass over both sides (the test's own text).
# Run by ``python -c`` that DuckDB takes itself for an interactive one, and prints a progress bar of each
# statement over two seconds on the output the test reads: turned off.
TEN_MILLION = """
WRITE = '''
import duckdb, sys
con = duckdb.connect(":memory:", config={"temp_directory": "/var/lib/deltasleuth/files/tmp"})
con.execute("SET enable_progress_bar = false")
for name, every in (("a", 0), ("b", 100000)):
    email = "'user' || n || '@example.com'"
    if every:
        email = f"CASE WHEN n % {every} = 0 THEN upper({email}) ELSE {email} END"
    con.execute(
        f"COPY (SELECT n AS userid, {email} AS email, 'Name ' || n AS name, CAST(n * 1.25 AS DECIMAL(12, 2))"
        f" AS amount FROM range(1, 10000001) t(n) WHERE n <> {3 if every else 0} ORDER BY n)"
        f" TO '/var/lib/deltasleuth/files/root/{name}.parquet' (FORMAT parquet)"
    )
'''
async def main():
    subprocess.run([sys.executable, "-c", WRITE], check=True)
    plans = await planned("a.parquet", "b.parquet", "userid")
    peak, stop = [0, 0], threading.Event()
    def watch():
        while not stop.wait(0.2):
            made = duck._instance
            if made is not None:
                try:
                    now = [status(made.proc.pid, name) for name in ("VmData", "VmHWM")]
                    peak[:] = [max(peak[0], now[0]), max(peak[1], now[1])]
                except (OSError, TypeError):
                    pass
    threading.Thread(target=watch, daemon=True).start()
    began = time.monotonic()
    async with engine.snapshot(exports) as a, engine.snapshot(exports) as b:
        sides = [await engine.scan(a, plans["a"], None), await engine.scan(b, plans["b"], None)]
        rows = [sum([len(batch) async for batch in side]) for side in sides]
    took = time.monotonic() - began
    stop.set()
    made = duck._instance
    print("rows", rows, "| data limit", made.limit, "| the reader's VmData peaked at", peak[0] // 1024,
          "MiB, its resident size at", peak[1] // 1024, "MiB |", round(took), "s | ended:", made.cause)
asyncio.run(main())
"""
F12 = (
    r"ReaderDied: side a: the file reader's process ended 3 times \((SIG[A-Z]+|exit [0-9]+)\) while only this"
    r" side's files were being read: they are read no more until DeltaSleuth restarts"
)


@pytest.fixture(scope="module")
def built() -> str:
    """The image's architecture, as Docker names it (``amd64``, ``arm64``)."""
    found = subprocess.run(
        ["docker", "image", "inspect", "-f", "{{.Architecture}}", IMAGE],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return found.stdout.strip()


@pytest.fixture(scope="module")
def native(built: str) -> bool:
    """Whether the image's machine is this host's: else it runs under emulation."""
    host, machines = os.uname().machine, {"amd64": "x86_64", "arm64": "aarch64"}
    return machines[built] == machines.get(host, host)


@pytest.fixture
def ran(
    tmp_path: Path, built: str, native: bool
) -> Iterator[Callable[..., subprocess.CompletedProcess[str]]]:
    """Run a text in the image under the compose hardening with no network, its volumes removed afterwards:
    ``PRELUDE`` first unless it is ``whole``; ``options`` are ``docker run``'s."""
    name, config = f"ds-reader-{secrets.token_hex(4)}", tmp_path / "etc"
    config.mkdir(mode=0o755)
    targets = yaml.safe_dump({"targets": TARGETS})
    settings = SETTINGS + ("" if native else "files: {os_limits: false}\n")
    for file, text in {"deltasleuth.yaml": settings, "targets.yaml": targets, "db_dsn": "x\n"}.items():
        (config / file).write_text(text)
        (config / file).chmod(0o644)
    volumes = {f"{name}-evidence": "/var/lib/deltasleuth/evidence", f"{name}-files": FILES}
    mounts = ["-v", f"{config}:/etc/deltasleuth:ro"]
    mounts += [option for volume, path in volumes.items() for option in ("-v", f"{volume}:{path}")]

    def run(text: str, *options: str, whole: bool = False, timeout: float = 300) -> Any:
        text = text if whole else f"LIMITS = {native}\n{PRELUDE}{text}"
        argv = ["docker", "run", "--rm", "--pull", "never", "--network", "none", "--name", name]
        argv += ["--platform", f"linux/{built}"]
        try:
            return subprocess.run(
                [*argv, *release.HARDENED, *mounts, *options, IMAGE, "python", "-c", text],
                capture_output=True, text=True, timeout=timeout,
            )  # fmt: skip
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            raise

    try:
        yield run
    finally:
        subprocess.run(["docker", "volume", "rm", "-f", *volumes], capture_output=True)


def _line(ran: subprocess.CompletedProcess[str]) -> str:
    """The one line a text printed; its error output, should it have failed."""
    assert ran.returncode == 0, ran.stderr[-2000:]
    (line,) = ran.stdout.splitlines()
    return line


ENDED = "the file reader's process ended (%s) with no statement in flight"


def test_the_self_test_passes(ran: Callable[..., Any], native: bool) -> None:
    """U32, U35 and U36: the reader starts with nothing of its parent's, proves its guard, reads and spills;
    it is replaced when a fault ends it; natively its start saw the data limit hold and sockets denied
    (which stopped nothing the self-test does), and a statement that holds twice a data limit of 512 MiB
    does not complete; no descriptor is inherited. Under emulation its limits are not tried, and the
    emulator's own descriptors are counted and warned of."""
    text = release.DUCK if native else release.DUCK.replace("()", "(limits=False)")
    done = ran(text, whole=True)
    held = "data limit held, sockets denied" if native else "limits not tried"
    assert _line(done) == (
        f"file reader: duckdb {version('duckdb')} in a process of its own, replaced when it ended; {held}"
    )
    # Its error output: the end of the reader it faulted and, where the small limit ends the other one
    # under DuckDB's allocator, of that one; nothing else, and natively no inherited descriptor.
    warned = [line for line in done.stderr.splitlines() if native or "inherited" not in line]
    assert warned[0] == ENDED % "SIGSEGV" and len(warned) <= 2
    assert all(re.fullmatch(re.escape(ENDED) % r"(SIG[A-Z]+|exit [0-9]+)", line) for line in warned)


def test_a_fault_ends_the_reader_s_process_alone(ran: Callable[..., Any]) -> None:
    """U33: SIGSEGV sent mid-statement, three times: ``Died``, ``Died`` read apart, then F12; the process
    that read (PID 1 here) lives, and a new reader reads another file."""
    done = ran(
        PLANNED
        + """
async def ended(plan):
    session = await engine.connect(exports, timeout_s=30)
    sent = asyncio.ensure_future(session.send(plan, duck.ENDLESS, {}))
    while not sent.done() and (
        session.cursor is None or session.cursor._flight not in session.cursor._instance._pending
    ):
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.3)
    made = session.cursor._instance
    os.kill(made.proc.pid, signal.SIGSEGV)
    try:
        await sent
    except tables.TableError as exc:
        return type(exc).__name__ + ": " + str(exc)
    finally:
        made._ending.join(10)
        await session.close()
async def main():
    (root / "other.parquet").write_bytes(duck.PARQUET)
    plans = await planned("two.parquet", "two.parquet", "id")
    ends = [await ended(plans["a"]) for _ in range(3)]
    other = (await planned("other.parquet", "other.parquet", "id"))["a"]
    print(" | ".join(ends), "|", await scanned(other), "rows by a new reader | pid", os.getpid())
asyncio.run(main())
"""
    )
    ends = _line(done).split(" | ")
    assert ends[:2] == ["Busy: Died", "Busy: Died"] and re.fullmatch(F12, ends[2])
    assert "(SIGSEGV)" in ends[2] and ends[3:] == ["2 rows by a new reader", "pid 1"]


def test_the_crafted_page_is_contained(ran: Callable[..., Any], record_property: Callable[..., None]) -> None:
    """U33: the kept mutant, which travels in the text. Its plan passes; what three scans of it do on this
    kernel is recorded, never judged: each is a read, a closed refusal or an end of the reader's process.
    The process that read lives, and a clean file is read after."""
    page = base64.b64encode(crafted.PAGE).decode()
    done = ran(
        PLANNED
        + f"""
async def main():
    (root / "crafted.parquet").write_bytes(base64.b64decode("{page}"))
    plan = (await planned("crafted.parquet", "crafted.parquet", "id"))["a"]
    ends = []
    for _ in range(3):
        try:
            ends.append(f"read {{await scanned(plan)}} rows")
        except tables.TableError as exc:
            cause = duck._charged.get(duck._set(plan), (0, ""))[1]
            ends.append(f"{{type(exc).__name__}}: {{exc}}" + (f" ({{cause}})" if str(exc) == "Died" else ""))
    clean = (await planned("two.parquet", "two.parquet", "id"))["a"]
    print("the crafted page: a plan of", plan["rows"], "rows; its scans:", "; ".join(ends),
          "; a clean file after:", await scanned(clean), "rows; pid", os.getpid())
asyncio.run(main())
"""
    )
    line = _line(done)
    record_property("crafted", line)
    print(line)
    found = re.fullmatch(
        r"the crafted page: a plan of 4 rows; its scans: (.+) ; a clean file after: 2 rows; pid 1", line
    )
    assert found is not None
    closed = rf"read [0-9]+ rows|Busy: Died \((SIG[A-Z]+|exit [0-9]+)\)|{F12}|TargetRefused: [A-Za-z]+"
    assert all(re.fullmatch(closed, end) for end in found[1].split("; "))


def test_no_reader_outlives_its_parent(ran: Callable[..., Any]) -> None:
    """U34: the end of its input is the reader's own, whatever ended its parent and whatever it was doing:
    an exit, SIGTERM, SIGKILL while it is idle and in the middle of a sort. Its spill directory goes with
    the next sweep."""
    done = ran(
        """
PARENT = '''
import asyncio, sys
from pathlib import Path
from deltasleuth.config import load_settings
from deltasleuth.connectors.tables import duck
root, how, limit = Path("/var/lib/deltasleuth/files/root"), sys.argv[1], int(sys.argv[2]) or None
made = duck._spawned(load_settings().spill_dir, [str(root)], root.parent / "outside.csv", 256, 1024, 2, limit)
async def read():
    cursor = made.cursor()
    await cursor.execute("SELECT 1")
    print(made.proc.pid, flush=True)
    if how == "sort":
        await cursor.execute(duck.SPILL_SORT.replace("200000", "300000000"))
    elif how != "exit":
        await asyncio.sleep(60)
asyncio.run(read())
'''
def gone(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:
        return True
outlived = []
KILL = signal.SIGKILL
for how, sent in (("exit", None), ("idle", signal.SIGTERM), ("idle", KILL), ("sort", KILL)):
    limit = str(duck._limit(256, 2) if LIMITS else 0)
    parent = subprocess.Popen([sys.executable, "-c", PARENT, how, limit], stdout=subprocess.PIPE, text=True)
    pid = int(parent.stdout.readline())
    if sent is not None:
        time.sleep(0.5)
        assert not gone(pid)
        parent.send_signal(sent)
    assert parent.wait(30) == (-sent if sent else 0)
    ended = time.monotonic() + 10
    while time.monotonic() < ended and not gone(pid):
        time.sleep(0.01)
    if not gone(pid):
        outlived.append(how)
left, swept = len(list(settings.spill_dir.iterdir())), manifest.sweep(settings.spill_dir)
print("outlived its parent:", outlived, "| directories left:", left, "| swept:", swept)
"""
    )
    assert _line(done) == "outlived its parent: [] | directories left: 1 | swept: 1"


def test_a_command_leaves_no_reader_behind(ran: Callable[..., Any], native: bool) -> None:
    """§5, §17: ``ds target check`` of a files target, run three times beside the container's first process
    as ``docker compose exec`` runs a command. Each kills and reaps its reader before it exits: none is
    left to the first process, which waits for no process it did not start, as a zombie entry. Each
    command's error output holds its reader's start line, and its first line what that start saw hold."""
    done = ran(
        """
argv = [str(Path(sys.executable).with_name("ds")), "target", "check", "exports"]
argv += ["--table", "two.parquet", "--format", "parquet"]
checks = [subprocess.run(argv, capture_output=True, text=True) for _ in range(3)]
time.sleep(1)  # a reader left to end by itself has ended
states = []
for name in os.listdir("/proc"):
    if name.isdigit():
        try:
            states.append(Path(f"/proc/{name}/stat").read_text().rsplit(")", 1)[1].split()[0])
        except OSError:
            pass
exits = [check.returncode for check in checks]
started = [check.stderr.count("the file reader started: duckdb ") for check in checks]
print("exits", exits, "| zombies", states.count("Z"), "| start lines", started, "|",
      checks[0].stdout.splitlines()[0])
"""
    )
    off = "files.os_limits is false"
    limits = "data limit 5,296 MiB, sockets denied"
    held = limits if native else f"no data limit ({off}), sockets not denied ({off})"
    said = (
        f"target exports: files, writable, duckdb {version('duckdb')}, parquet and core_functions built in,"
        f" no extension loads, read in a process of its own ({held})"
    )
    assert _line(done) == f"exits [0, 0, 0] | zombies 0 | start lines [1, 1, 1] | {said}"


def test_idle_cursors_hold_no_thread(ran: Callable[..., Any], native: bool) -> None:
    """U35: 200 open cursors, each with a result half read, add no thread to the reader's process, and the
    self-test's sort completes beside them: natively under a data limit of 1 GiB, which 200 threads' stacks
    alone would pass."""
    done = ran(
        """
async def main():
    made = start(duck.SELFTEST_MIB, 1024)
    first = made.cursor()
    await first.execute("SELECT 1")
    before = status(made.proc.pid, "Threads")
    cursors = [made.cursor() for _ in range(200)]
    for cursor in cursors:
        await cursor.execute("SELECT i FROM range(10) t(i)")
        assert await cursor.fetch(1) == [(0,)]
    async with asyncio.timeout(5):
        while (held := status(made.proc.pid, "Threads")) != before:
            await asyncio.sleep(0.05)
    spilled = await duck._spilled(first, made.spill / "temp")
    print("data limit", made.limit, "| no thread more:", held == before, "| spilled", spilled)
    duck._stopped(made)
asyncio.run(main())
"""
    )
    assert _line(done) == f"data limit {1024 if native else None} | no thread more: True | spilled True"


def test_the_default_limit_fits_ten_million_rows(
    ran: Callable[..., Any], native: bool, record_property: Callable[..., None]
) -> None:
    """U35: a pass over 10M rows a side of FILES §17's shape (four columns, Parquet against Parquet, both
    sides' sorts held at once) under the default ``files`` settings and their data limit completes; the
    reader's peak ``VmData`` and resident size are recorded."""
    if not native:
        pytest.skip("a data limit would count the emulator")
    done = ran(PLANNED + TEN_MILLION, timeout=1500)
    line = _line(done)
    record_property("ten_million", line)
    print(line)
    found = re.fullmatch(
        r"rows \[10000000, 9999999\] \| data limit 5296 \| the reader's VmData peaked at ([0-9]+) MiB, its"
        r" resident size at [0-9]+ MiB \| [0-9]+ s \| ended: None",
        line,
    )
    assert found is not None and 0 < int(found[1]) < 5296


def test_the_reader_leaves_no_core(ran: Callable[..., Any], record_property: Callable[..., None]) -> None:
    """U37: with DuckDB loaded and a statement running, the reader's core limit is 0 and it is not dumpable:
    SIGSEGV ends it without a core, where a process started beside it with no such restriction leaves one
    (recorded: whether the kernel there writes cores at all)."""
    done = ran(
        """
work = manifest.held(settings.spill_dir)
proc = subprocess.Popen(
    [sys.executable, "-I", "-X", "utf8", reader.__file__], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL, env={}, close_fds=True, start_new_session=True, cwd=work,
)
def tell(**message):
    proc.stdin.write(reader.pack(message, reader.REQUEST_MAX))
    proc.stdin.flush()
def answer():
    return reader.unpack(reader.read(proc.stdout, {b"J": reader.ANSWER_MAX})[1])
limit = duck._limit(256, 2) if LIMITS else None
tell(roots=[str(root)], spill=str(work), memory=256, spilled=1024, threads=2, limit=limit, filter=LIMITS,
     frame_max=reader.FRAME_MAX, probe=str(probe))
ready = answer()
tell(id=1, op="execute", cursor=1, statement=duck.ENDLESS, binds=None)
time.sleep(0.5)
limits = re.search(r"Max core file size\\s+(\\S+)\\s+(\\S+)", Path(f"/proc/{proc.pid}/limits").read_text())
dumpable = os.stat(f"/proc/{proc.pid}/status").st_uid == os.getuid()
os.kill(proc.pid, signal.SIGSEGV)
_, ended = os.waitpid(proc.pid, 0)
left = sorted(name for name in os.listdir(work) if "core" in name)
CONTROL = "import os, resource, signal; hard = resource.getrlimit(resource.RLIMIT_CORE)[1];" \\
    " resource.setrlimit(resource.RLIMIT_CORE, (hard, hard)); os.kill(os.getpid(), signal.SIGSEGV)"
other = subprocess.Popen([sys.executable, "-c", CONTROL], cwd=work, stderr=subprocess.DEVNULL)
_, control = os.waitpid(other.pid, 0)
print("ready", ready["ready"], "| core limit", limits[1], limits[2], "| dumpable", dumpable, "| ended by",
      signal.Signals(os.WTERMSIG(ended)).name, "| core", os.WCOREDUMP(ended), left,
      "| an unrestricted process there left a core:", os.WCOREDUMP(control),
      "| core_pattern", Path("/proc/sys/kernel/core_pattern").read_text().strip())
manifest.release(work)
"""
    )
    line = _line(done)
    record_property("core", line)
    print(line)
    assert line.startswith(
        "ready True | core limit 0 0 | dumpable False | ended by SIGSEGV | core False [] |"
    )


def test_the_kernel_takes_the_reader_first(ran: Callable[..., Any]) -> None:
    """U37: in a container of 512 MiB a reader that outgrows it (2 GiB of DuckDB memory, a statement that
    holds about 1 GiB; its data limit lies above the container) is the process the kernel takes: the
    statement is ``Died``, the cause SIGKILL, and the process that read exits 0."""
    done = ran(
        """
async def main():
    made = start(2048)
    adjusted = Path(f"/proc/{made.proc.pid}/oom_score_adj").read_text().strip()
    cursor = made.cursor()
    try:
        await cursor.execute(duck.HOLDS)
        met = f"completed: {await cursor.fetch(1)}"
    except tables.TableError as exc:
        met = f"{type(exc).__name__}: {exc}"
    made._ending.join(10)
    print("oom_score_adj", adjusted, "|", met, f"({made.cause})", "| pid", os.getpid())
    duck._stopped(made)
asyncio.run(main())
""",
        "--memory", "512m", "--memory-swap", "512m",
    )  # fmt: skip
    assert _line(done) == "oom_score_adj 1000 | Busy: Died (SIGKILL) | pid 1"
