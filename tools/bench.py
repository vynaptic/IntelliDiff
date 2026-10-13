"""``bench``: the maintainer's benchmark harness (docs/mvp/BENCHMARK.md), dsv's and release's sibling, outside
the shipped product.

    uv run python tools/bench.py run --out DIR [--rows 100k,1M,10M] [--scenarios sparse,dense,clustered,graph]
        [--shape narrow|wide] [--repeat 3] [--server file:/abs/admin-dsn] [--work DIR] [--timeout 240]
        [--stall 10] [--max-rss BYTES] [--allow-dirty]
    uv run python tools/bench.py report FILE... [--draft] [--extrapolate ROWS]
    uv run python tools/bench.py clean [--server file:/abs/admin-dsn] [--work DIR]

``run`` starts a throwaway postgres:16-alpine (never pulled), or takes a dedicated server, generates
deterministic tables in it with generate_series, runs the product's own ``ds run`` on a fresh control plane
per repetition, gates each repetition on exact ground truth and rewrites one deltasleuth-bench/1 result file
after each; ``report`` renders result files as Markdown under the claim rules (§8); ``clean`` recovers from a
kill. It removes only the containers it labelled and drops only the databases it marked. Every child gets one
scrubbed environment, and the database password stays in 0600 files, never in an argv, ``docker inspect`` or
the environment of the harness or its children; the image's entrypoint exports it, though, so the container's
postmaster keeps it in its environment while the container runs, readable by root or uid 70 where the
container runs (Docker Desktop's VM on darwin) and through ``docker exec``. Every number is observed on stated
hardware, synthetic data.
"""

from __future__ import annotations

import contextlib
import getpass
import hashlib
import json
import math
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import version as installed
from pathlib import Path
from typing import Annotated, Any

import psycopg
import typer
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from deltasleuth.core.secrets import SecretError, read_secret
from deltasleuth.evidence import fsio
from deltasleuth.runner import scan
from deltasleuth.runner.worker import READ_BATCH
from tools import dsv, release
from tools.dsv import fail, file_ref
from tools.release import POSTGRES, SYNCED, _run

FORMAT = "deltasleuth-bench/1"
LABEL, MARKER, MARK = "deltasleuth.bench", ".deltasleuth-bench", "deltasleuth-bench"  # container, folder, db
DATA = "/var/lib/postgresql/data"
WORKER = {"concurrency": 4, "lease_seconds": 120, "poll_seconds": 0.05}  # D4
DIRTY = ("deltasleuth", "tools", "pyproject.toml", "uv.lock")  # what a result's commit must hold (B3)
LOOPBACK = ("localhost", "127.0.0.1", "::1")
POLL_S, SETTLE_S, QUIET_S, SLEPT_S, STOP_S = 0.1, 1, 60, 5, 30
MIN_KEYS = 2000  # rates under this many keys are null
ITEM = re.compile(r"[1-9][0-9]*[kM]?")
TERMINAL = ("cancelled", "completed", "completed_with_issues", "failed")
SCANNED = ("rows_a", "rows_b", "skipped_a", "skipped_b", "matched", "candidates")
OUTCOMES = ("MATCH", "MISMATCH", "NOT_FOUND_A", "NOT_FOUND_B", "KNOWN_DIFF", "NOISE_ONLY", "DRIFT",
            "INCONCLUSIVE")  # fmt: skip
GRAPH = ("groups_a", "groups_b", "groups_equal", "groups_different", "split", "merge", "rearrange",
         "oversized")  # fmt: skip
STATS = ("temp_files", "temp_bytes", "blks_read", "blks_hit", "tup_returned")
SETTINGS = ("shared_buffers", "work_mem", "maintenance_work_mem", "max_wal_size", "synchronous_commit",
            "fsync", "jit", "max_parallel_workers_per_gather", "temp_file_limit")  # fmt: skip
# What postgres:16-alpine shows for SETTINGS.
STOCK = {"shared_buffers": "128MB", "work_mem": "4MB", "maintenance_work_mem": "64MB", "max_wal_size": "1GB",
         "synchronous_commit": "on", "fsync": "on", "jit": "on", "max_parallel_workers_per_gather": "2",
         "temp_file_limit": "-1"}  # fmt: skip
STORE = ("findings", "observations", "results", "run_keys")  # the store tables a cycle end joins
UNCLEAN = "database system was not properly shut down"
PAUSED = re.compile(r"scan of run \S+ paused: (\w+)$")
# §3's estimates, in bytes: a row of each table with its indexes, a row a side of each table's sort temp held
# at once (a link fact's L1 sorts end before its L2 sorts begin, so the larger phase), and a differing key in
# the store and in the evidence folder. Each size's re-check takes those the smaller sizes measured.
ESTIMATES = {"users.narrow": 110, "users.wide": 450, "links": 150, "temp.users": 80, "temp.links": 150,
             "key": 5000, "evidence": 12000}  # fmt: skip
CAVEAT = (
    "What these numbers do not show: synthetic data on one host; both sources and the control plane on one"
    ' PostgreSQL server, with the settings the conditions line names; no cold-cache figure ("restarted" means'
    " the server restarted with its OS page cache warm); worker.poll_seconds 0.05, where the shipped 1 s can"
    " add about a second to each cycle's end and each idle slot's next lease; politeness budgets lifted,"
    " where production reads at most rate_per_s × 200 keys a second per side; the service crawl, rate-bound"  # noqa: RUF001
    " at T = N·(1+m·t)/r, not benchmarked; vendor-measured, not independently verified."
)
NOT_MEASURED = (
    "Not measured: the service crawl and mixed pairs, rate-bound at T = N·(1+m·t)/r (architecture §12.1);"
    " remote sources beyond rtt_ms; a cold cache; soak re-diffs; concurrent runs; other engines; scale-out"
    "; the release image; the accuracy draw and the readiness measurement."
)
NOTES = {
    "d4": "worker {concurrency: 4, lease_seconds: 120, poll_seconds: 0.05}: the shipped concurrency and"
    " lease, the DS_PERF poll, so it measures work rather than idle polling (D4)",
    "darwin": "darwin: os.fsync does not use F_FULLFSYNC, so writes reach the disk's cache, not stable"
    " storage",
    "forward": "the database is reached through Docker's port forward on 127.0.0.1, which each round trip"
    " pays",
    "cache": "sources fit in the page cache: reads are memory reads",
    "restarted": "restarted: the server restarted before each scenario's first repetition at a size, shared"
    " buffers empty, OS page cache warm (the Docker VM's or the host's); not a cold cache",
    "d5": "the table scenarios differ from the DS_PERF table test (TABLES §7): key class identifier, not"
    " business_code; execution.concurrency 8, not the spec default; lease 120 s, not 30 s; side b generated,"
    " not UPDATEd and DELETEd, so no dead tuples; frozen tables",
}
app = typer.Typer(
    no_args_is_help=True, help="The maintainer's benchmark harness.", pretty_exceptions_enable=False
)
ServerOpt = Annotated[str | None, typer.Option(help="file:/abs of a dedicated PostgreSQL 16's admin DSN.")]
WorkOpt = Annotated[Path | None, typer.Option(help="The work folder's parent (default: the temp directory).")]


def _names(cls: type) -> set[str]:
    return {cls.__name__}.union(*(_names(sub) for sub in cls.__subclasses__()))


OPERATIONAL = _names(psycopg.OperationalError)  # what the worker retries without end (§6 step 4)


@dataclass(frozen=True)
class Scenario:
    """A pattern of §4: its table, side b's selected keys and what it does with each (SQL over the key g, or
    a link fact's entity e), and its truth in closed form."""

    table: str
    selected: str
    kind: str
    truth: Callable[[int], dict[str, int]]


def _counts(n: int, *, rows_b: int, mismatch: int, nf_a: int, nf_b: int, **graph: int) -> dict[str, int]:
    matched = n - mismatch - nf_b
    scanned = dict(zip(SCANNED, (n, rows_b, 0, 0, matched, mismatch + nf_a + nf_b), strict=True))
    found = {"MATCH": matched, "MISMATCH": mismatch, "NOT_FOUND_A": nf_a, "NOT_FOUND_B": nf_b}
    return scanned | dict.fromkeys(OUTCOMES, 0) | found | graph


def _graph(n: int) -> dict[str, int]:
    groups, m = 3 * (n // 2) // 2 + 1, n // 10_000  # E phones and E/2 + 1 emails; M = E/5,000 moved
    found = dict(zip(GRAPH, (groups, groups, groups - 2 * m, 2 * m, 0, 0, m, 0), strict=True))
    return _counts(n, rows_b=n, mismatch=0, nf_a=m, nf_b=m, **found)


def _changed(n: int) -> dict[str, int]:
    return _counts(n, rows_b=n, mismatch=n // 125, nf_a=n // 1000, nf_b=n // 1000)  # 0.8, 0.1, 0.1 of N/100


SCENARIOS = {
    "sparse": Scenario("users", "g % 100000 = 0 OR g = 3", "CASE WHEN g = 3 THEN 0 ELSE 2 END",
                       lambda n: _counts(n, rows_b=n - 1, mismatch=n // 100_000, nf_a=0, nf_b=1)),
    "dense": Scenario("users", "g % 100 = 50", "(g / 100) % 10", _changed),
    "clustered": Scenario("users", "(g - 1) % ({n} / 10) < {n} / 1000", "g % 10", _changed),
    "graph": Scenario("links", "e % 5000 = 2500", "", _graph),
}  # fmt: skip
NARROW = ("userid bigint", "email text", "name text", "amount numeric(12, 2)")
WIDE = (*(f"t{k} text" for k in range(1, 9)), *(f"n{k} numeric(14, 2)" for k in range(1, 5)),
        *(f"i{k} integer" for k in range(1, 5)), "d1 date", "d2 date", "b1 boolean",
        "b2 boolean")  # fmt: skip
SHAPES = {"narrow": NARROW, "wide": NARROW + WIDE}
SIZES = {  # count, table and index bytes, and the mean row of the first 10,000 keys
    "users": "SELECT (SELECT count(*) FROM users), pg_table_size('users'), pg_indexes_size('users'),"
    " (SELECT avg(pg_column_size(t.*))::int FROM users t WHERE userid <= 10000)",
    "links": "SELECT (SELECT count(*) FROM links), pg_table_size('links'), pg_indexes_size('links'),"
    " (SELECT avg(pg_column_size(t.*))::int FROM (SELECT * FROM links LIMIT 10000) t)",
}


def truth(scenario: str, n: int) -> dict[str, int]:
    """What a correct run of ``scenario`` at ``n`` rows a side counts (§4)."""
    return SCENARIOS[scenario].truth(n)


def _row(x: str, shape: str) -> list[str]:
    """The plain row of key ``x`` (SQL): side a's, and side b's unless changed; wide columns are alike."""
    row = [x, f"'user' || {x} || '@example.com'", f"'Name ' || {x}", f"{x} * 1.25"]
    if shape == "wide":  # x is bigint, so x * k holds at 10^9
        row += [f"md5(({x} * {k})::text)" for k in range(1, 9)] + [f"{x} * {k} / 100.0" for k in range(1, 5)]
        row += [f"({x} * {k}) % 2147483647" for k in range(1, 5)]
        row += [f"date '2000-01-01' + (({x} * {k}) % 9000)::int" for k in (1, 2)]
        row += [f"({x} + {k}) % 2 = 0" for k in (1, 2)]
    return row


def _statements(side: str, scenario: str, n: int, shape: str) -> list[str]:
    """Create, fill by one INSERT … SELECT … FROM generate_series, and key a side's table of ``scenario``."""
    found, series = SCENARIOS[scenario], f"FROM generate_series(1::bigint, {n}) g"
    if found.table == "links":  # fact g is entity (g + 1) / 2's phone or email; side b moves some emails
        moved = f" + CASE WHEN {found.selected} THEN 1 ELSE 0 END" if side == "b" else ""
        return ["CREATE TABLE links (entity_code text, id_type text, id_value text)",
                "INSERT INTO links SELECT 'e' || e, CASE WHEN g % 2 = 1 THEN 'phone' ELSE 'email' END,"
                f" CASE WHEN g % 2 = 1 THEN 'p' || e ELSE 'x' || (e / 2{moved}) END {series}"
                " CROSS JOIN LATERAL (SELECT (g + 1) / 2 AS e) f",
                "ALTER TABLE links ADD PRIMARY KEY (entity_code, id_type, id_value)",
                "CREATE INDEX ON links (id_type, id_value)"]  # fmt: skip
    plain = _row("g", shape)
    fill = f"SELECT {', '.join(plain)} {series}"
    if side == "b":  # by kind: 0 absent; 1 alike, plus key N + g; 2-5 email; 6-7 name; 8-9 amount
        selected, email, name, amount = found.selected.format(n=n), *plain[1:4]
        changed = [plain[0], f"CASE WHEN c BETWEEN 2 AND 5 THEN upper({email}) ELSE {email} END",
                   f"CASE WHEN c IN (6, 7) THEN {name} || ' Jr' ELSE {name} END",
                   f"CASE WHEN c IN (8, 9) THEN {amount} + 0.01 ELSE {amount} END", *plain[4:]]  # fmt: skip
        extra = ", ".join(_row(f"({n} + g)", shape))
        fill = (f"SELECT {', '.join(changed)} {series} CROSS JOIN LATERAL (SELECT CASE WHEN {selected} THEN"  # noqa: S608
                f" {found.kind} END AS c) k WHERE c IS DISTINCT FROM 0 UNION ALL SELECT {extra} {series}"
                f" WHERE ({selected}) AND {found.kind} = 1")  # fmt: skip
    return [f"CREATE TABLE users ({', '.join(SHAPES[shape])})", f"INSERT INTO users {fill}",
            "ALTER TABLE users ADD PRIMARY KEY (userid)"]  # fmt: skip


@dataclass(frozen=True)
class Limits:
    timeout_min: int
    stall_min: int
    max_rss: int  # bytes


@dataclass(frozen=True)
class Options:
    out: Path
    rows: list[int]
    scenarios: list[str]
    shape: str
    repeat: int
    server: tuple[str, str] | None  # its file: reference and the admin DSN
    parent: Path  # of the work folder
    limits: Limits
    allow_dirty: bool
    ds: Path


def sizes(text: str, option: str = "--rows") -> list[int]:
    """The sizes of ``text`` (``100k,1M,10M``), smallest first (B6)."""
    found: list[int] = []
    for item in text.split(","):
        value = 0
        if ITEM.fullmatch(item):
            value = int(item[:-1]) * (1000 if item[-1] == "k" else 10**6) if item[-1] in "kM" else int(item)
        if value % 10_000 or not 10_000 <= value <= 10**9 or value in found:
            fail(f"{option} {item}: sizes are multiples of 10,000 from 10,000 to 1,000,000,000, each once")
        found.append(value)
    return sorted(found)


def _label(rows: int) -> str:
    return f"{rows // 10**6}M" if rows % 10**6 == 0 else f"{rows // 1000}k"


def _gb(size: float) -> str:
    return f"{size / 1e9:.1f}"


def _memory() -> int:
    return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")


def _work_parent(parent: Path) -> None:
    """B4: the work folder's parent is a local folder outside every git working tree."""
    if not parent.is_dir():
        fail(f"--work {parent} is not a folder")
    path = parent.resolve()
    if any((folder / ".git").exists() for folder in (path, *path.parents)):
        fail(f"--work {parent} is in a git working tree: evidence would sit beside the code")
    for synced in SYNCED:  # as release._outside: by name, or by identity (APFS ignores case)
        folder = Path.home() / synced
        if any(path.is_relative_to(p) for p in (folder, folder.resolve())) or (
            folder.is_dir() and any(os.path.samefile(p, folder) for p in (path, *path.parents))
        ):
            fail(f"--work {parent} is in a cloud-synced folder (~/{synced}): use a local folder")


def _hosts(params: dict[str, Any]) -> list[str]:
    return [h for key in ("host", "hostaddr") for h in str(params.get(key) or "").split(",") if h]


def _remote(params: dict[str, Any]) -> bool:
    return any(not h.startswith("/") and h not in LOOPBACK for h in _hosts(params))


def _admin(ref: str) -> str:
    """The server's admin DSN (B8), self-contained, since libpq fills what it omits from the caller's PG*
    environment for the harness's own connections: a host or hostaddr, no service, and no hostaddr left to
    PGHOSTADDR or PGSERVICE; refused where it would cross a network unverified (B14)."""
    try:
        dsn = read_secret(file_ref(ref, "--server"))
    except SecretError as exc:
        fail(f"--server: {exc}")
    try:
        params = conninfo_to_dict(dsn)
    except psycopg.ProgrammingError:  # its message would quote the DSN, password included
        fail("--server: the admin DSN is not a valid connection string")
    if "service" in params:
        fail("--server: the admin DSN names a service: write its parameters in the file")
    if not _hosts(params):
        fail("--server: the admin DSN names no host: give host= or hostaddr= (a socket folder counts), or"
             " libpq takes PGHOST")  # fmt: skip
    for name in ("PGHOSTADDR", "PGSERVICE"):
        if name in os.environ and "hostaddr" not in params:
            fail(f"--server: {name} is set and the admin DSN names no hostaddr: the harness's own connections"
                 f" would take it; unset {name}, or give hostaddr=")  # fmt: skip
    fallbacks = {"PGPORT": "port", "PGUSER": "user", "PGPASSWORD": "password", "PGSSLMODE": "sslmode"}
    for name, key in fallbacks.items():
        if name in os.environ and key not in params:
            fail(f"--server: {name} is set and the admin DSN names no {key}: the harness's own connections"
                 f" would take it; unset {name}, or give {key}=")  # fmt: skip
    if _remote(params) and (mode := params.get("sslmode") or "prefer") != "verify-full":
        fail(f"the server's DSN names a remote host with sslmode {mode}: use sslmode=verify-full, since the"
             " benchmark connects as a superuser")  # fmt: skip
    return dsn


def _refuse_arguments(
    out: Path, rows: str, scenarios: str, shape: str, server: str | None, work: Path | None, **given: Any
) -> Options:
    """Stage 1: the arguments, before any subprocess."""
    parent = work or Path(tempfile.gettempdir())
    _work_parent(parent)
    ds = Path(sys.executable).with_name("ds")  # the checkout's
    if not ds.is_file():
        fail(f"ds is not installed next to {sys.executable}: run uv sync")
    chosen = sizes(rows)
    for item in (names := scenarios.split(",")):
        if item not in SCENARIOS:
            fail(f"--scenarios {item}: choose from {', '.join(SCENARIOS)}")
    if shape not in SHAPES:
        fail("--shape must be narrow or wide")
    admin = (server, _admin(server)) if server else None
    if not out.is_dir():
        fail(f"--out {out} is not a folder")
    picked = [name for name in SCENARIOS if name in names]  # the table pairs first, then the links
    return Options(out, chosen, picked, shape, server=admin, parent=parent, ds=ds, **given)


def _env(endpoint: str | None = None) -> dict[str, str]:
    """The one environment every child gets (§3): the caller's without PG*, DS_*, DOCKER_HOST and
    DOCKER_CONTEXT, and the resolved Docker endpoint as DOCKER_HOST."""
    dropped = ("DOCKER_HOST", "DOCKER_CONTEXT")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PG", "DS_")) and k not in dropped}
    return env | ({"DOCKER_HOST": endpoint} if endpoint else {})


def _endpoint() -> str:
    """The Docker endpoint, resolved once with the caller's DOCKER_HOST and DOCKER_CONTEXT (B1)."""
    caller = _env() | {k: os.environ[k] for k in ("DOCKER_HOST", "DOCKER_CONTEXT") if k in os.environ}
    absent = "docker is not available: install Docker, or give --server file:<absolute path>"
    if shutil.which("docker", path=caller.get("PATH")) is None:
        fail(absent)
    argv = ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]
    done = _run(argv, check=False, env=caller)
    if done.returncode or not (host := done.stdout.strip()):
        fail(absent)
    if not host.startswith(("unix://", "npipe://")):
        fail(f"the Docker endpoint is not a local socket ({host.partition('://')[0]}://): the port and the"
             " password file would be on another machine; use a local Docker, or --server file:<absolute"
             " path>")  # fmt: skip
    return host


def _dirty(env: dict[str, str]) -> list[str]:
    """What git reports under deltasleuth/, tools/, pyproject.toml and uv.lock, untracked files included."""
    argv = ["git", "status", "--porcelain", "--untracked-files=all", "--", *DIRTY]
    return _run(argv, cwd=release.ROOT, env=env).stdout.splitlines()


def _marked(name: str, comment: str | None) -> str | None:
    """The run id a database is marked with, when its name and its comment agree."""
    found = re.fullmatch(r"dsbench_([0-9a-f]{8})_(?:a|b|store|run)", name)
    return found[1] if found and comment == f"{MARK} {found[1]}" else None


def _connect(dsn: str, attempts: int = 60) -> psycopg.Connection[Any]:
    """An autocommit connection, tried ``attempts`` times half a second apart (a port forward restarting)."""
    for attempt in range(attempts):
        try:
            return psycopg.connect(dsn, autocommit=True, connect_timeout=10)
        except psycopg.OperationalError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.5)
    raise AssertionError("unreachable")


DATABASES = "SELECT datname, shobj_description(oid, 'pg_database') FROM pg_database"


def _server_guard(ref: str, admin: str) -> None:
    """B10, B9 and B11: a superuser, on a server holding only the templates and marked databases."""
    try:
        with _connect(admin, attempts=1) as conn:
            superuser = conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()
            rows = conn.execute(DATABASES).fetchall()
    except psycopg.OperationalError as exc:
        fail(f"the server is unreachable ({type(exc).__name__}): check --server's DSN")
    if not (superuser and superuser[0]):
        fail("the server's role is not a superuser: the benchmark creates, checkpoints and drops databases")
    marked = [name for name, comment in rows if _marked(name, comment)]
    if other := [n for n, _ in rows if n not in ("postgres", "template0", "template1", *marked)]:
        fail(f"the server holds {len(other)} other databases: give the benchmark a PostgreSQL server of its"
             " own")  # fmt: skip
    if marked:
        fail(f"{len(marked)} benchmark databases exist: another run is under way or was killed; if none is"
             f" running, run: uv run python tools/bench.py clean --server {ref}")  # fmt: skip


def _refuse_environment(opts: Options) -> tuple[dict[str, str], dict[str, Any]]:
    """Stage 2: the environment, before any container, database or file. The children's environment, and
    the commit, dirt and image found."""
    env, image = _env(None if opts.server else _endpoint()), None
    if opts.server is None:
        found = _run(["docker", "image", "inspect", "--format", "{{.Id}}", POSTGRES], check=False, env=env)
        if found.returncode:
            fail(f"{POSTGRES} (the benchmark's database) is not in the local image store; this tool never"
                 f" pulls. To add it: docker pull {POSTGRES}")  # fmt: skip
        image = found.stdout.strip()
    sha, dirty = (
        _run(["git", "rev-parse", "HEAD"], cwd=release.ROOT, env=env).stdout.strip(),
        bool(_dirty(env)),
    )
    if dirty and not opts.allow_dirty:
        fail("deltasleuth/, tools/, pyproject.toml or uv.lock has uncommitted or untracked changes: commit"
             " them, or pass --allow-dirty (the result is then not publishable)")  # fmt: skip
    if opts.server:
        _server_guard(*opts.server)
    else:
        argv = ["docker", "ps", "-a", "--filter", f"label={LABEL}", "--format", "{{.Names}}"]
        if names := _run(argv, env=env).stdout.split():
            fail(f"a benchmark container ({names[0]}) exists: another run is under way or was killed; if none"
                 " is running, run: uv run python tools/bench.py clean")  # fmt: skip
    return env, {"image_id": image, "sha": sha, "dirty": dirty}


def _free_port() -> int:
    with socket.socket() as s:  # as tests/conftest.py's _free_port
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Container:
    """The throwaway postgres:16-alpine: labelled, published on a loopback port fixed across restarts, its
    superuser's random password in a 0600 file that the entrypoint reads as root (D8)."""

    topology = "container"

    def __init__(self, bench_id: str, work: Path, env: dict[str, str]) -> None:
        self.id, self.name, self.work, self.env = bench_id, f"dsbench-{bench_id}", work, env
        self.port, self.password = _free_port(), ""

    def _docker(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return _run(["docker", *args], check=check, env=self.env)

    def start(self) -> None:
        self.password = secrets.token_urlsafe(24)
        dsv.create(self.work / "pg_password", f"{self.password}\n".encode(), 0o600)
        self._docker("run", "-d", "--pull", "never", "--name", self.name, "--label", f"{LABEL}={self.id}",
                     "-p", f"127.0.0.1:{self.port}:5432", "--shm-size", "1g", "-v",
                     f"{self.work}/pg_password:/run/secrets/pg_password:ro", "-e",
                     "POSTGRES_PASSWORD_FILE=/run/secrets/pg_password", POSTGRES)  # fmt: skip
        self._ready()

    def _ready(self) -> None:
        deadline, ready = time.monotonic() + 120, ("pg_isready", "-q", "-h", "127.0.0.1", "-U", "postgres")
        while self._docker("exec", self.name, *ready, check=False).returncode:
            if time.monotonic() > deadline:
                fail(f"{self.name} did not accept connections within 120 s")
            time.sleep(0.5)

    def dsn(self, db: str = "postgres") -> str:  # hostaddr too: libpq would take PGHOSTADDR for it
        return make_conninfo(host="127.0.0.1", hostaddr="127.0.0.1", port=self.port, user="postgres",
                             password=self.password, dbname=db, sslmode="disable")  # fmt: skip

    def free(self) -> int | None:
        """Free bytes on the database volume."""
        return (
            int(self._docker("exec", self.name, "df", "-Pk", DATA).stdout.splitlines()[-1].split()[3]) * 1024
        )

    def restart(self, started: Callable[[], datetime]) -> bool | None:
        """Restart the server; whether cleanly: ``started()``, the postmaster's start, advanced, and its log
        since holds no crash recovery."""
        before, since = started(), f"{time.time() - 1:.3f}"
        self._docker("restart", "-t", "300", self.name)
        self._ready()
        logs = self._docker("logs", "--since", since, self.name)
        return started() > before and UNCLEAN not in logs.stdout + logs.stderr

    def cpu_usec(self) -> int | None:
        stat = self._docker("exec", self.name, "cat", "/sys/fs/cgroup/cpu.stat", check=False).stdout
        return int(found[1]) if (found := re.search(r"^usage_usec (\d+)$", stat, re.M)) else None

    def stopped(self) -> str:
        """Why the database stopped answering: docker inspect's State."""
        state = "{{.State.OOMKilled}} {{.State.Running}} {{.State.ExitCode}}"
        found = self._docker("inspect", "--format", state, self.name, check=False).stdout.split()
        oom, running, code = [*found, "", "", ""][:3]
        return "out of memory" if oom == "true" else f"exited {code}" if running == "false" else "unreachable"

    def others(self) -> dict[str, Any]:
        """The other containers' count and CPU, never their names."""
        listed = self._docker(
            "stats", "--no-stream", "--format", "{{.Name}}\t{{.CPUPerc}}", check=False
        ).stdout
        cpu = [p.strip().rstrip("%") for n, _, p in (line.partition("\t") for line in listed.splitlines())]
        cpu = [
            p
            for p, line in zip(cpu, listed.splitlines(), strict=True)
            if line.partition("\t")[0] != self.name
        ]
        return {"containers": len(cpu), "cpu_pct": round(sum(float(p) for p in cpu if _number(p)), 1)}

    def docker(self) -> dict[str, Any]:
        info = json.loads(self._docker("info", "--format", "{{json .}}").stdout)
        keys = {"version": "ServerVersion", "cpus": "NCPU", "memory_bytes": "MemTotal",
                "cgroup": "CgroupVersion", "os": "OperatingSystem"}  # fmt: skip
        return {name: info.get(key) for name, key in keys.items()}

    def remove(self) -> None:
        self._docker("rm", "-f", "-v", self.name, check=False)  # -v: the image's VOLUME


def _number(text: str) -> bool:
    return text.replace(".", "", 1).isdigit()


class Server:
    """A dedicated PostgreSQL 16: each DSN is the admin's with another dbname, so its TLS parameters stay."""

    def __init__(self, admin: str, bench_id: str) -> None:
        self.admin, self.id = admin, bench_id
        self.topology = "remote" if _remote(conninfo_to_dict(admin)) else "loopback"

    def dsn(self, db: str | None = None) -> str:
        return self.admin if db is None else make_conninfo(self.admin, dbname=db)

    def start(self) -> None:
        pass

    def free(self) -> int | None:
        return None  # not checked

    def restart(self, started: Callable[[], datetime]) -> bool | None:
        return None  # no restart: cache "first"

    def cpu_usec(self) -> int | None:
        return None

    def stopped(self) -> str:
        return "unreachable"

    def others(self) -> dict[str, Any] | None:
        return None

    def docker(self) -> dict[str, Any] | None:
        return None

    def remove(self) -> None:
        with contextlib.suppress(psycopg.OperationalError):
            _drop_marked(self.admin, self.id)


def _drop_marked(dsn: str, bench_id: str | None = None) -> int:
    """Drop the databases marked as ``bench_id``'s (or as any run's), never another; how many."""
    with _connect(dsn, attempts=1) as conn:
        found = [
            n for n, c in conn.execute(DATABASES).fetchall() if (m := _marked(n, c)) and bench_id in (None, m)
        ]
        for name in found:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
    return len(found)


@dataclass
class Bench:
    id: str
    db: Container | Server
    work: Path
    env: dict[str, str]
    opts: Options
    path: Path  # the result file
    result: dict[str, Any] = field(default_factory=dict)
    constants: dict[str, float] = field(default_factory=lambda: dict(ESTIMATES))
    failed: set[str] = field(default_factory=set)  # scenarios not run at larger sizes
    code: int = 0

    def database(self, part: str) -> str:
        return f"dsbench_{self.id}_{part}"

    def admin(self) -> psycopg.Connection[Any]:
        return _connect(self.db.dsn())


class _Stopped(Exception):
    """The benchmark database stopped: nothing more can run."""


def _create(conn: psycopg.Connection[Any], bench: Bench, part: str, template: str | None = None) -> None:
    name = sql.Identifier(bench.database(part))
    like = sql.SQL(" TEMPLATE {}").format(sql.Identifier(template)) if template else sql.SQL("")
    conn.execute(sql.SQL("CREATE DATABASE {}").format(name) + like)
    conn.execute(sql.SQL("COMMENT ON DATABASE {} IS {}").format(name, sql.Literal(f"{MARK} {bench.id}")))


def _specs() -> dict[str, dict[str, Any]]:
    """bench-tables (table_spec's shape) and bench-graph (graph_spec), as tests/e2e writes them (§5)."""
    recheck = {"delay": "0s", "times": 1, "outcomes": ["MISMATCH", "NOT_FOUND_A", "NOT_FOUND_B"]}
    base = {"apiVersion": "deltasleuth/v1", "kind": "ComparisonSpec", "normalizerVersion": "1.1.0",
            "compare": {}, "recheck": recheck}  # fmt: skip

    def spec(name: str, key: dict[str, Any], table: dict[str, Any], **more: Any) -> dict[str, Any]:
        sides = {
            s: {"role": r, "target": f"db-{s}", "table": table} for s, r in (("a", "legacy"), ("b", "new"))
        }
        return base | {"metadata": {"name": name, "version": "1.0.0", "owner": "bench"}, "key": key,
                       "sides": sides} | more  # fmt: skip

    parts = ["entity_code", "id_type", "id_value"]
    return {
        "bench-tables": spec("bench-tables", {"name": "userid", "type": "string", "class": "identifier"},
                             {"name": "users", "keyColumns": ["userid"]}, execution={"concurrency": 8},
                             security={"unlisted": "tokenize", "clear": ["$.amount"]}),
        "bench-graph": spec("bench-graph", {"name": "link", "type": "composite", "parts": parts,
                                            "class": "identifier"},
                            {"name": "links", "keyColumns": parts, "columns": ["entity_code"]},
                            graph={"members": ["entity_code"], "groups": parts[1:], "maxComponent": 20},
                            security={"unlisted": "tokenize"}),
    }  # fmt: skip


def _files(work: str) -> dict[str, dict[str, Any]]:
    """What ds reads (§5), with ``work`` the work folder ("<work>" in the result's profile)."""
    settings = {"database_dsn_ref": f"file:{work}/dsn_run", "evidence_dir": f"{work}/evidence",
                "workspace_dir": f"{work}/workspace", "targets_file": f"{work}/targets.json",
                "worker": WORKER}  # fmt: skip
    target = {"rate_per_s": 1000, "max_concurrency": 4}
    return {
        "settings.json": settings,
        "settings-store.json": settings | {"database_dsn_ref": f"file:{work}/dsn_store"},
        "targets.json": {"targets": {f"db-{s}": {"dsn_ref": f"file:{work}/dsn_{s}"} | target for s in "ab"}},
        **{f"{name}.json": doc for name, doc in _specs().items()},
    }


def _masker(work: Path) -> Callable[[str], str]:
    """Masks a line of ds's output: the work folder, home, the host's and the user's names."""
    pairs = [(str(work), "<work>"), (str(Path.home()), "~"), (socket.gethostname(), "<host>"),
             (getpass.getuser(), "<user>")]  # fmt: skip

    def mask(line: str) -> str:
        for old, new in pairs:
            line = line.replace(old, new) if old else line
        return line

    return mask


def _rss_bytes(maxrss: int, system: str = sys.platform) -> int:
    """ru_maxrss in bytes: darwin counts bytes, Linux KiB."""
    return maxrss if system == "darwin" else maxrss * 1024


def _reap(proc: subprocess.Popen[bytes], *, block: bool = False) -> Any:
    """The child's rusage once it exited (None before), reaped by os.wait4, its exit code set at once: Popen
    would otherwise wait for a pid the system may have reused."""
    pid, status, usage = os.wait4(proc.pid, 0 if block else os.WNOHANG)
    if not pid:
        return None
    proc.returncode = os.waitstatus_to_exitcode(status)
    return usage


def _stop(proc: subprocess.Popen[bytes]) -> Any:
    """SIGINT, then SIGKILL after 30 s (os.kill: Popen's send_signal would reap it); the child's rusage."""
    os.kill(proc.pid, signal.SIGINT)
    deadline = time.monotonic() + STOP_S
    while (usage := _reap(proc)) is None:
        if time.monotonic() > deadline:
            os.kill(proc.pid, signal.SIGKILL)
            return _reap(proc, block=True)
        time.sleep(POLL_S)
    return usage


def _ds(bench: Bench, *args: str) -> Any:
    """``ds ARGS`` in the work folder, with the one environment; its rusage. A failure ends the run."""
    log = bench.work / f"ds-{args[0]}.log"
    with log.open("wb") as out:
        argv = [str(bench.opts.ds), *args]
        proc = subprocess.Popen(argv, env=bench.env, cwd=bench.work, stdout=out, stderr=subprocess.STDOUT)  # noqa: S603
    usage = _reap(proc, block=True)
    if proc.returncode:
        last = (log.read_text(errors="replace").strip().splitlines() or [""])[-1]
        fail(f"ds {args[0]} failed (exit {proc.returncode}): {_masker(bench.work)(last)}")
    return usage


def _ps(pid: int, env: dict[str, str]) -> int | None:
    out = _run(["ps", "-o", "rss=", "-p", str(pid)], check=False, env=env).stdout.strip()
    return int(out) * 1024 if out.isdigit() else None


OPEN = "FROM ds.run_keys k WHERE k.run_id = r.run_id AND k.cycle = r.cycle AND k.state IN ('pending', 'leased', 'recheck')"  # noqa: E501
POLL = (
    "SELECT r.status, (s.state->>'done')::boolean, (s.state->>'checkpoint')::int, {open}, EXISTS (SELECT 1"
    " FROM pg_stat_activity a WHERE a.datname = ANY(%s) AND a.state = 'active' AND a.backend_type = 'client"
    " backend'), extract(epoch FROM clock_timestamp() - r.created_at)::float8,"
    " (SELECT coalesce(sum(size), 0) FROM pg_ls_tmpdir())::bigint"
    " FROM ds.runs r LEFT JOIN ds.scans s ON s.run_id = r.run_id AND s.cycle = r.cycle"
)
POLLED = ("status", "done", "checkpoint", "open", "source", "elapsed", "temp")


class _Poll:
    """The watch's one query on the run's store: status, done, checkpoint, the open keys (whether any, or
    with ``count`` how many: run_keys_open_idx), whether a source backend is active, the database's seconds
    since the run's insert, and the temp file bytes the server holds now (pg_ls_tmpdir: a superuser's). A
    lost connection is opened again once; lost twice, it raises."""

    def __init__(self, dsn: str, sources: list[str]) -> None:
        self.dsn, self.sources, self.conn = dsn, sources, None

    def __enter__(self) -> _Poll:
        return self

    def __exit__(self, *exc: object) -> None:
        if self.conn is not None:
            self.conn.close()

    def __call__(self, count: bool = False) -> dict[str, Any] | None:
        try:
            return self._query(count)
        except psycopg.OperationalError:
            with contextlib.suppress(psycopg.Error, AttributeError):
                self.conn.close()  # type: ignore[union-attr]
            self.conn = None
            return self._query(count)

    def _query(self, count: bool) -> dict[str, Any] | None:
        if self.conn is None:
            self.conn = _connect(self.dsn, attempts=1)
        opened = f"(SELECT count(*) {OPEN})" if count else f"EXISTS (SELECT 1 {OPEN})"
        row = self.conn.execute(POLL.format(open=opened), (self.sources,)).fetchone()  # type: ignore[arg-type]
        return None if row is None else dict(zip(POLLED, row, strict=True))


@dataclass
class Watched:
    """What the watch saw: the host times of the phases, the database's run_s, the most temp file bytes held
    at once, the abort, ds's stderr."""

    t: dict[str, float]
    run_s: float | None = None
    temp_peak: int = 0
    open_at_done: int | None = None
    rss_done: int | None = None
    abort: str | None = None
    usage: Any = None
    lines: list[str] = field(default_factory=list)


def _read(w: Watched, err: Path, mask: Callable[[str], str], repeated: Counter[str]) -> str | None:
    """ds's new stderr lines, masked, into ``w.lines``; the abort a worker line calls for (§6 step 4)."""
    text = err.read_text(errors="replace")
    lines = [mask(line) for line in text.splitlines()[: text.count("\n")]]  # whole lines only
    new, w.lines = lines[len(w.lines) :], lines
    for line in (line for line in new if not line.startswith("{")):  # not an audit row
        if found := PAUSED.search(line):  # the scan retries each lease period without end
            return f"scan paused: {found[1]}"
        if line.rpartition(": ")[2] in OPERATIONAL:  # a slot, the sweep, a save or a draw retrying
            repeated[line] += 1
            if repeated[line] == 3:
                return f"{line} (3 times)"
    return None


def _watch(
    proc: subprocess.Popen[bytes],
    poll: Callable[..., dict[str, Any] | None],
    clock: Callable[[], float],
    *,
    err: Path,
    limits: Limits,
    stopped: Callable[[], str],
    env: dict[str, str],
    mask: Callable[[str], str] = str,
) -> Watched:
    """Watch ``ds run`` until it exits or aborts (§6 steps 3 and 4): ``poll`` every 0.1 s and os.wait4; each
    second a count of the open keys, ps and ds's new stderr. t_progress, t_done, t_drained and t_end are
    ``clock``'s times of the polls that first saw them; an aborted child gets SIGINT, then SIGKILL."""
    w, repeated = Watched({"start": clock()}), Counter[str]()
    last = tick = w.t["start"]
    state: tuple[Any, ...] | None = None
    fewest: int | None = None

    def seen(row: dict[str, Any] | None, now: float, counted: bool) -> None:
        nonlocal state, last, fewest
        if row is None:
            return  # ds has not created the run yet
        w.temp_peak = max(w.temp_peak, row.get("temp") or 0)
        if (key := (row["status"], row["checkpoint"], row["done"])) != state:
            state, last = key, now
        if row["source"] and "done" not in w.t:  # a sort, or L2's lookups
            last = now
        if counted and (fewest is None or row["open"] < fewest):
            fewest, last = row["open"], now
        if "progress" not in w.t and ((row["checkpoint"] or 0) >= 1 or row["done"]):
            w.t["progress"] = now
        if row["done"] and "done" not in w.t:
            row = poll(count=True) or row  # t_done's one count of the open keys
            w.t["done"], w.open_at_done, w.rss_done = now, row["open"], _ps(proc.pid, env)
        if "done" in w.t and "drained" not in w.t and not row["open"]:
            w.t["drained"] = now
        if row["status"] in TERMINAL and "end" not in w.t:
            w.t["end"], w.run_s = now, round(row["elapsed"], 3)

    while w.abort is None:
        now = clock()
        second = now - tick >= 1
        try:
            seen(poll(count=second), now, second)
        except psycopg.OperationalError:
            w.abort = f"the benchmark database stopped ({stopped()})"
            break
        if (usage := _reap(proc)) is not None:
            w.usage, w.t["reaped"] = usage, clock()
            if "end" not in w.t:
                with contextlib.suppress(psycopg.OperationalError):
                    seen(poll(), clock(), False)
            break
        if second:
            tick, w.abort, rss = now, _read(w, err, mask, repeated), _ps(proc.pid, env)
            if w.abort is None and rss is not None and rss > limits.max_rss:
                w.abort = f"peak memory {_gb(rss)} GB over --max-rss {_gb(limits.max_rss)} GB"
        if w.abort is None and now - w.t["start"] > limits.timeout_min * 60:
            w.abort = f"timeout after {limits.timeout_min} min"
        if w.abort is None and "drained" not in w.t and now - last > limits.stall_min * 60:
            line = next((x for x in reversed(w.lines) if not x.startswith("{")), "no worker line")
            w.abort = f"stalled: no progress for {limits.stall_min} min ({line})"
        if w.abort is None:
            time.sleep(POLL_S)
    if w.usage is None:
        w.usage, w.t["reaped"] = _stop(proc), clock()
    _read(w, err, mask, Counter())
    if w.abort is None and (code := proc.returncode):
        w.abort = f"killed by signal {-code}" if code < 0 else f"ds exited {code}"
    return w


BUSY = (
    "SELECT count(*) FROM pg_stat_activity WHERE pid <> pg_backend_pid() AND (backend_type = 'autovacuum"
    " worker' OR (backend_type = 'client backend' AND state = 'active'))"
)
RUN = (
    "SELECT r.status, r.counters, s.state - 'after' FROM ds.runs r"
    " LEFT JOIN ds.scans s ON s.run_id = r.run_id AND s.cycle = r.cycle"
)
OBSERVED = (
    "SELECT count(*), extract(epoch FROM max(observed_at) - min(observed_at))::float8 FROM ds.observations"
    " WHERE side = 'a' AND pass_no = 1"
)
ANALYSED = (  # each store table's last analyses against finished_at, the cycle end's start (§2)
    "SELECT extract(epoch FROM r.finished_at - r.created_at)::float8, t.relname,"
    " extract(epoch FROM t.last_analyze - r.finished_at)::float8,"
    " extract(epoch FROM t.last_autoanalyze - r.finished_at)::float8"
    " FROM ds.runs r CROSS JOIN pg_stat_user_tables t WHERE t.schemaname = 'ds' AND t.relname = ANY(%s)"
    " ORDER BY t.relname"
)


def _power(env: dict[str, str]) -> str | None:
    if sys.platform != "darwin":
        return None
    first = (_run(["pmset", "-g", "batt"], check=False, env=env).stdout.splitlines() or [""])[0]
    return "ac" if "AC Power" in first else "battery" if "Battery Power" in first else None


def _quiet(bench: Bench) -> dict[str, Any]:
    """Step 1: a checkpoint, then up to 60 s for no other active client backend or autovacuum worker."""
    started = time.monotonic()
    with bench.admin() as conn:
        conn.execute("CHECKPOINT")
        while conn.execute(BUSY).fetchone()[0] and time.monotonic() - started < QUIET_S:  # type: ignore[index]
            time.sleep(0.5)
    waited, load = round(time.monotonic() - started, 1), round(os.getloadavg()[0], 2)
    return {"quiet_wait_s": waited, "load_1m": load, "power": _power(bench.env), "others": bench.db.others()}


def _stats(conn: psycopg.Connection[Any], bench: Bench) -> dict[str, dict[str, int]]:
    names = [bench.database(s) for s in "ab"]
    query = sql.SQL("SELECT datname, {} FROM pg_stat_database WHERE datname = ANY(%s)")
    rows = conn.execute(query.format(sql.SQL(", ").join(map(sql.Identifier, STATS))), (names,))
    found = {row[0]: dict(zip(STATS, row[1:], strict=True)) for row in rows}
    return {s: found[name] for s, name in zip("ab", names, strict=True)}


def _span(start: float | None, end: float | None) -> float | None:
    return None if start is None or end is None else round(end - start, 3)


def _epoch(stamp: str | None) -> float | None:
    return None if not stamp else datetime.fromisoformat(stamp).timestamp()


def _judge(rep: dict[str, Any], expected: dict[str, int], counters: dict[str, Any]) -> list[str]:
    """Why a repetition is not correct (§6 step 6), an abort kept apart: a status, a counter against the
    truth, a snapshot, an identity, a sleep."""
    seen, slept = rep["observed"], rep["noise"]["slept_s"]
    wrong = [] if rep["status"] == "completed" else [f"status {rep['status']}, truth completed"]
    wrong += [f"{k} {seen.get(k)}, truth {v}" for k, v in expected.items() if seen.get(k) != v]
    wrong += [f"{k} {seen.get(k)}, truth 1" for k in ("snapshots_a", "snapshots_b") if seen.get(k) != 1]
    wrong += [f"reconciliation {i['identity']}: {i['left']}, truth {i['right']}"
              for i in counters.get("reconciliation", []) if not i["holds"]]  # fmt: skip
    return wrong + ([f"host slept {slept} s"] if slept >= SLEPT_S else [])


def repetition(
    bench: Bench, rows: int, scenario: str, n: int, cache: str, clean: bool | None
) -> tuple[dict[str, Any], list[str]]:
    """One repetition (§6): quiet, a fresh store from the template, ``ds run`` watched, its measures read,
    the store dropped, judged against the truth; with ds's last 5 stderr lines, masked, to print."""
    wall, mono, run_db, store = time.time(), time.monotonic(), bench.database("run"), bench.database("store")
    rep: dict[str, Any] = {"n": n, "cache": cache, "restart_clean": clean, "correct": False, "problems": [],
                           "aborted": None, "exit": None, "status": None, "noise": _quiet(bench)}  # fmt: skip
    with bench.admin() as conn:
        _create(conn, bench, "run", template=store)
        before = _stats(conn, bench)
    cpu, graph, err = bench.db.cpu_usec(), SCENARIOS[scenario].table == "links", bench.work / "ds-run.err"
    spec, settings = bench.work / f"bench-{'graph' if graph else 'tables'}.json", bench.work / "settings.json"
    with err.open("wb") as stderr, (bench.work / "ds-run.out").open("wb") as stdout:
        argv = [str(bench.opts.ds), "run", str(spec), "--config", str(settings)]
        proc = subprocess.Popen(argv, env=bench.env, cwd=bench.work, stdout=stdout, stderr=stderr)  # noqa: S603
    try:
        with _Poll(bench.db.dsn(run_db), [bench.database(s) for s in "ab"]) as poll:
            w = _watch(proc, poll, time.time, err=err, limits=bench.opts.limits, stopped=bench.db.stopped,
                       env=bench.env, mask=_masker(bench.work))  # fmt: skip
    except BaseException:
        if proc.returncode is None:
            _stop(proc)
        raise
    rep |= {"exit": proc.returncode, "aborted": w.abort}
    if w.abort and w.abort.startswith("the benchmark database stopped"):
        rep["noise"]["slept_s"] = None
        return rep, w.lines[-5:]
    time.sleep(SETTLE_S)
    with bench.admin() as conn:
        after = _stats(conn, bench)
        grown = conn.execute("SELECT pg_database_size(%s) - pg_database_size(%s)", (run_db, store)).fetchone()
    used = bench.db.cpu_usec()
    with _connect(bench.db.dsn(run_db)) as conn:
        status, counters, state = conn.execute(RUN).fetchone() or (None, {}, None)  # type: ignore[misc]
        keys, span = conn.execute(OBSERVED).fetchone()  # type: ignore[misc]
        analysed = conn.execute(ANALYSED, (list(STORE),)).fetchall()
    files = [p for p in (bench.work / "evidence").rglob("*") if p.is_file()]
    evidence = sum(p.stat().st_size for p in files)
    shutil.rmtree(bench.work / "evidence", ignore_errors=True)  # emptied after each repetition
    with bench.admin() as conn:
        conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(run_db)))
    state, counters, t, opened = state or {}, counters or {}, w.t, w.open_at_done
    first, graph_at, by = (
        _epoch(state.get("first_at")),
        _epoch(state.get("graph_at")),
        counters.get("by_outcome", {}),
    )
    observed = {k: state.get(k) for k in SCANNED} | {o: by.get(o) for o in OUTCOMES}
    observed |= {k: (state.get("graph") or {}).get(k) for k in GRAPH if graph}
    observed |= {f"snapshots_{s}": state.get(f"snapshots_{s}") for s in "ab"}
    tail = _span(t.get("done"), t.get("drained"))
    seconds = {"run": w.run_s, "first_progress": _span(first, t.get("progress")),
               "scan": _span(first, t.get("done")), "l1": _span(first, graph_at),
               "l2": _span(graph_at, t.get("done")), "reads_tail": tail,
               "cycle_end": _span(t.get("drained"), t.get("end")),
               "process": _span(t["start"], t.get("reaped"))}  # fmt: skip
    reads = {"keys": keys, "span_s": span and round(span, 3), "open_at_scan_end": opened,
             "keys_per_s": round((keys - READ_BATCH) / span, 1) if keys >= MIN_KEYS and span else None,
             "tail_keys_per_s": round(opened / tail, 1) if (opened or 0) >= MIN_KEYS and tail else None,
             }  # fmt: skip
    ds_s = round(w.usage.ru_utime + w.usage.ru_stime, 2)
    database_s = None if cpu is None or used is None else round((used - cpu) / 1e6, 2)
    rep |= {"status": status, "observed": observed, "seconds": seconds, "reads": reads,
            "memory": {"peak_rss_bytes": _rss_bytes(w.usage.ru_maxrss), "rss_scan_end_bytes": w.rss_done},
            "cpu": {"ds_s": ds_s, "database_s": database_s},
            "sources": {s: {k: after[s][k] - before[s][k] for k in STATS} for s in "ab"},
            "temp_peak_bytes": w.temp_peak,
            "stored": {"store_bytes": grown[0], "evidence_bytes": evidence, "evidence_files": len(files)},  # type: ignore[index]
            "statistics": {"finished_s": analysed[0][0] if analysed else None, "tables": {
                t: {"analyze_s": a, "autoanalyze_s": b} for _, t, a, b in analysed}}}  # fmt: skip
    rep["noise"]["slept_s"] = round((time.time() - wall) - (time.monotonic() - mono), 1)
    wrong = _judge(rep, truth(scenario, rows), counters)
    rep["correct"] = not wrong and not rep["aborted"]
    rep["problems"] = wrong + (["restart was not clean (crash recovery)"] if clean is False else [])
    return rep, w.lines[-5:]


def generate(bench: Bench, side: str, scenario: str, rows: int) -> dict[str, Any]:
    """Side's table of ``scenario`` in its database (§4): logged, filled in a session with synchronous_commit
    off and 1GB of maintenance_work_mem, keyed, frozen and analysed, then a checkpoint, so no measured read
    writes hint bits; its count must be the truth's. Its sizes."""
    table, started = SCENARIOS[scenario].table, time.monotonic()
    with _connect(bench.db.dsn(bench.database(side))) as conn:
        conn.execute("SET synchronous_commit = off; SET maintenance_work_mem = '1GB'")
        for statement in _statements(side, scenario, rows, bench.opts.shape):
            conn.execute(statement)  # type: ignore[arg-type]
        conn.execute(sql.SQL("VACUUM (FREEZE, ANALYZE) {}").format(sql.Identifier(table)))
        conn.execute("CHECKPOINT")
        generated = round(time.monotonic() - started, 1)
        count, table_bytes, index_bytes, row_bytes = conn.execute(SIZES[table]).fetchone()  # type: ignore[arg-type,misc]
    if count != (expected := rows if side == "a" else truth(scenario, rows)["rows_b"]):
        raise RuntimeError(f"side {side}'s {table} holds {count} rows, not the truth's {expected}")
    columns = len(SHAPES[bench.opts.shape]) if table == "users" else 3
    return {"rows": count, "columns": columns, "table_bytes": table_bytes, "index_bytes": index_bytes,
            "row_bytes": row_bytes, "generated_s": generated}  # fmt: skip


def _drop(bench: Bench, side: str, table: str) -> None:
    with _connect(bench.db.dsn(bench.database(side))) as conn:
        conn.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier(table)))


def _started(bench: Bench) -> datetime:
    with bench.admin() as conn:
        return conn.execute("SELECT pg_postmaster_start_time()").fetchone()[0]  # type: ignore[index]


def _line(rows: int, scenario: str, rep: dict[str, Any], repeat: int, lines: list[str]) -> str:
    head = f"{_label(rows)} {scenario} {rep['n']}/{repeat} ({rep['cache']})"
    if rep["aborted"]:
        return "\n".join([f"{head}: aborted ({rep['aborted']})", *(f"  {line}" for line in lines)])
    if not rep["correct"]:
        return f"{head}: incorrect ({rep['problems'][0]})"
    s, rate, peak = rep["seconds"], rep["reads"]["keys_per_s"], rep["memory"]["peak_rss_bytes"] / 1e6
    return (f"{head}: run {s['run']:.1f} s (scan {s['scan']:.1f} s, {'-' if rate is None else f'{rate:,.0f}'}"
            f" keys/s, cycle end {s['cycle_end']:.1f} s), peak {peak:.0f} MB, correct")  # fmt: skip


def _scenario(bench: Bench, rows: int, scenario: str, entry: dict[str, Any]) -> None:
    """A scenario's repetitions at a size, the server restarted before the first; one not correct ends them
    and the scenario's larger sizes."""
    record = {"scenario": scenario, "truth": truth(scenario, rows), "repetitions": [], "summary": {}}
    entry["scenarios"].append(record)
    for n in range(1, bench.opts.repeat + 1):
        clean = bench.db.restart(lambda: _started(bench)) if n == 1 else None
        cache = "warm" if n > 1 else "first" if isinstance(bench.db, Server) else "restarted"
        rep, lines = repetition(bench, rows, scenario, n, cache, clean)
        record["repetitions"].append(rep)  # type: ignore[attr-defined]
        record["summary"] = summary(record["repetitions"])  # type: ignore[arg-type]
        _write(bench)
        typer.echo(_line(rows, scenario, rep, bench.opts.repeat, lines))
        if (rep["aborted"] or "").startswith("the benchmark database stopped"):
            raise _Stopped(rep["aborted"])
        if not rep["correct"]:
            bench.failed.add(scenario)
            bench.code = 1
            return


def _size(bench: Bench, rows: int, entry: dict[str, Any]) -> None:
    """One size: users a, then each table scenario with its own users b; then the links. A pair at a time."""
    held = []
    for table in ("users", "links"):
        chosen = [s for s in bench.opts.scenarios if SCENARIOS[s].table == table and s not in bench.failed]
        if not chosen:
            continue
        a = entry["tables"][f"{table}.a"] = generate(bench, "a", chosen[0], rows)
        for scenario in chosen:
            b = entry["tables"][f"{table}.b.{scenario}"] = generate(bench, "b", scenario, rows)
            held.append(sum(t["table_bytes"] + t["index_bytes"] for t in (a, b)))
            _scenario(bench, rows, scenario, entry)
            _drop(bench, "b", table)
        _drop(bench, "a", table)
    found = bench.result["environment"]  # Docker's MemTotal, or a loopback server's host's memory
    memory = (found["docker"] or {}).get("memory_bytes") or (
        found["host"]["memory_bytes"] if found["topology"] == "loopback" else None
    )
    entry["fits_in_memory"] = None if memory is None or not held else max(held) <= memory


def _need(bench: Bench, rows: int, c: dict[str, float]) -> tuple[float, float]:
    """Bytes ``rows`` needs on the database volume, 1.5 × (the tables held at once + both sides' sort temp
    held at once + the store per differing key), and in the work folder, 1.5 × the evidence per differing key
    (§3)."""  # noqa: RUF002
    tables = {SCENARIOS[s].table for s in bench.opts.scenarios}  # one pair at a time
    held = max(2 * c["links" if t == "links" else f"users.{bench.opts.shape}"] * rows for t in tables)
    temp = max(2 * c[f"temp.{t}"] * rows for t in tables)
    differing = max(truth(s, rows)["candidates"] for s in bench.opts.scenarios)
    return 1.5 * (held + temp + c["key"] * differing), 1.5 * c["evidence"] * differing


def _docker_data() -> Path | None:
    """Docker Desktop's data folder, where its disk image grows, when this is darwin and it exists."""
    found = Path.home() / "Library" / "Containers" / "com.docker.docker" / "Data"
    return found if sys.platform == "darwin" and found.is_dir() else None


def _same_disk(a: Path, b: Path) -> bool:
    return os.stat(a).st_dev == os.stat(b).st_dev


def _space(bench: Bench, rows: int, c: dict[str, float]) -> tuple[str, str] | None:
    """(B12's refusal, the re-check's abort) when ``rows`` does not fit, else None. On darwin Docker
    Desktop's disk image grows on the disk holding its data folder, which needs the database's bytes too:
    with the work folder's when they share that disk, or when there is no such folder."""
    db_need, work_need = _need(bench, rows, c)
    db_free, work_free = bench.db.free(), shutil.disk_usage(bench.work).free
    data = _docker_data() if db_free is not None else None
    both = db_free is not None and sys.platform == "darwin" and (data is None or _same_disk(data, bench.work))
    data_free = shutil.disk_usage(data).free if data is not None and not both else None
    work_want = work_need + db_need * both
    wanted = ((db_need, db_free), (db_need, data_free), (work_want, work_free))
    if (short := next(((n, f) for n, f in wanted if f is not None and n > f), None)) is None:
        return None
    label, work = _label(rows), f"{_gb(work_want)} GB in the work folder (it has {_gb(work_free)} GB"
    if db_free is None:
        refusal = f"{label} rows a side need about {work}): free space, or run fewer sizes"
    else:
        data_text = "" if data_free is None else (f", {_gb(db_need)} GB on the disk holding Docker Desktop's"
                                                  f" data (it has {_gb(data_free)} GB)")  # fmt: skip
        refusal = (f"{label} rows a side need about {_gb(db_need)} GB free on the database volume (it has"
                   f" {_gb(db_free)} GB){data_text} and {work}{'; on darwin it holds both' * both}): free"
                   " space, or run fewer sizes")  # fmt: skip
    return refusal, f"did not run: {label} rows need about {_gb(short[0])} GB free, {_gb(short[1])} GB left"


def _measured(bench: Bench, entry: dict[str, Any]) -> None:
    """The constants a size measured, for the larger sizes' re-checks: a row of each table with its indexes;
    the store and the evidence per differing key, from its repetition with the most; the most temp held at
    once per row a side, once a sort spilled (graph's L1 and L2 sorts write more, one phase after the
    other)."""
    per_row: dict[str, float] = {}
    for name, t in entry["tables"].items():
        kind = "links" if name.startswith("links") else f"users.{bench.opts.shape}"
        per_row[kind] = max(per_row.get(kind, 0), (t["table_bytes"] + t["index_bytes"]) / t["rows"])
    bench.constants |= per_row
    if reps := [rep for record in entry["scenarios"] for rep in record["repetitions"] if rep["correct"]]:
        most = max(reps, key=lambda rep: rep["observed"]["candidates"])
        stored, differing = most["stored"], most["observed"]["candidates"]
        bench.constants["key"] = stored["store_bytes"] / differing
        bench.constants["evidence"] = (stored["evidence_bytes"] + 4096 * stored["evidence_files"]) / differing
    temp: dict[str, float] = {}
    for record in entry["scenarios"]:
        kind = f"temp.{SCENARIOS[record['scenario']].table}"
        for rep in (rep for rep in record["repetitions"] if rep["correct"]):
            found = rep["temp_peak_bytes"] / (rep["observed"]["rows_a"] + rep["observed"]["rows_b"])
            temp[kind] = max(temp.get(kind, 0), found)
    bench.constants |= {kind: value for kind, value in temp.items() if value}  # once a sort spilled


def _host(env: dict[str, str]) -> dict[str, Any]:
    """This host's hardware and software, never its name."""
    model = cpu = None
    if sys.platform == "darwin":
        argv = ["sysctl", "-n", "hw.model", "machdep.cpu.brand_string"]
        model, cpu = _run(argv, env=env).stdout.splitlines()[:2]
    else:
        with contextlib.suppress(OSError):
            listed = Path("/proc/cpuinfo").read_text().splitlines()
            cpu = next(
                (line.split(":", 1)[1].strip() for line in listed if line.startswith("model name")), None
            )
        with contextlib.suppress(OSError):
            model = Path("/sys/devices/virtual/dmi/id/product_name").read_text().strip() or None
    libpq = psycopg.pq.version()
    return {"os": sys.platform, "model": model, "cpu": cpu, "cpus": os.cpu_count(), "memory_bytes": _memory(),
            "python": platform.python_version(), "psycopg": psycopg.__version__,
            "libpq": f"{libpq // 10000}.{libpq % 100}"}  # fmt: skip


def environment(bench: Bench, image_id: str | None) -> dict[str, Any]:
    """The host, Docker and server the numbers were observed on, and rtt_ms: the median of 200 SELECT 1."""
    with bench.admin() as conn:
        show = {n: conn.execute(sql.SQL("SHOW {}").format(sql.Identifier(n))).fetchone()[0] for n in SETTINGS}  # type: ignore[index]
        version, rtt = conn.execute("SHOW server_version").fetchone()[0], []  # type: ignore[index]
        for _ in range(200):
            started = time.perf_counter()
            conn.execute("SELECT 1")
            rtt.append(time.perf_counter() - started)
    image = None if isinstance(bench.db, Server) else POSTGRES
    postgres = {"image": image, "image_id": image_id, "version": version, "settings": show}
    return {"host": _host(bench.env), "docker": bench.db.docker(), "postgres": postgres,
            "topology": bench.db.topology, "rtt_ms": round(statistics.median(rtt) * 1000, 3)}  # fmt: skip


def _prepare(bench: Bench) -> int:
    """Once: the sides' and the store's databases, marked, and the files ds reads; ds init, then ds migrate
    on the store. ds migrate's peak RSS."""
    with bench.admin() as conn:
        for part in ("a", "b", "store"):
            _create(conn, bench, part)
    for part in ("a", "b", "store", "run"):
        dsv.create(bench.work / f"dsn_{part}", f"{bench.db.dsn(bench.database(part))}\n".encode(), 0o600)
    for name, doc in _files(str(bench.work)).items():
        (bench.work / name).write_text(json.dumps(doc, indent=1))
    _ds(bench, "init", "--config", str(bench.work / "settings.json"))
    return _rss_bytes(_ds(bench, "migrate", "--config", str(bench.work / "settings-store.json")).ru_maxrss)


def _profile(baseline: int) -> dict[str, Any]:
    files = _files("<work>")
    specs = {name: {"sha256": hashlib.sha256(json.dumps(doc, indent=1).encode()).hexdigest(), "document": doc}
             for name, doc in _specs().items()}  # fmt: skip
    return {"settings": files["settings.json"], "targets": files["targets.json"], "specs": specs,
            "read_batch": READ_BATCH, "scan_batch": scan.BATCH, "baseline_rss_bytes": baseline}  # fmt: skip


def _repetitions(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [rep for size in result["sizes"] for record in size["scenarios"] for rep in record["repetitions"]]


def _complete(result: dict[str, Any]) -> bool:
    o = result["options"]
    ran = {(s["rows"], r["scenario"]): len(r["repetitions"]) for s in result["sizes"] for r in s["scenarios"]}
    everything = all(ran.get((rows, s)) == o["repeat"] for rows in o["rows"] for s in o["scenarios"])
    return everything and "interrupted" not in result["problems"]


def _unpublishable(result: dict[str, Any]) -> list[str]:
    """Why a result is not publishable (§7), or nothing."""
    reps = _repetitions(result)
    reasons = {
        "incomplete": not result["complete"],
        "uncommitted changes": result["commit"]["dirty"],
        "a repetition not correct": any(not rep["correct"] for rep in reps),
        "on battery": any(rep["noise"].get("power") == "battery" for rep in reps),
        "the host slept": any((rep["noise"].get("slept_s") or 0) >= SLEPT_S for rep in reps),
        "an unclean restart": any(rep["restart_clean"] is False for rep in reps),
    }
    return [reason for reason, holds in reasons.items() if holds]


def _notes(result: dict[str, Any]) -> list[str]:
    env, container = result["environment"], result["environment"]["topology"] == "container"
    held = {"d4": True, "darwin": env["host"]["os"] == "darwin", "forward": container,
            "cache": any(size.get("fits_in_memory") for size in result["sizes"]), "restarted": container,
            "d5": any(SCENARIOS[s].table == "users" for s in result["options"]["scenarios"])}  # fmt: skip
    return [NOTES[name] for name, holds in held.items() if holds]


def _write(bench: Bench) -> None:
    """Rewrite the result file, atomically: counts, timings, sizes and environment only."""
    r = bench.result
    r["ended"], r["complete"], r["notes"] = f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ}", _complete(r), _notes(r)
    r["publishable"] = not _unpublishable(r)
    fsio.atomic_write(bench.path, (json.dumps(r, indent=1) + "\n").encode())


def _entry(rows: int, did_not_run: str | None = None) -> dict[str, Any]:
    return {"rows": rows, "fits_in_memory": None, "did_not_run": did_not_run, "tables": {}, "scenarios": []}


def _bench(bench: Bench, facts: dict[str, Any], started: datetime) -> int:
    """The run, once the database is up and every size fits: its exit code."""
    opts = bench.opts
    try:
        measured_on, baseline = environment(bench, facts["image_id"]), _prepare(bench)
        options = {"rows": opts.rows, "scenarios": opts.scenarios, "shape": opts.shape, "repeat": opts.repeat,
                   "server": opts.server is not None, "timeout_min": opts.limits.timeout_min,
                   "stall_min": opts.limits.stall_min, "max_rss_bytes": opts.limits.max_rss}  # fmt: skip
        bench.result = {
            "format": FORMAT, "bench_id": bench.id, "complete": False,
            "started": f"{started:%Y-%m-%dT%H:%M:%SZ}", "ended": None, "publishable": False, "problems": [],
            "commit": {"sha": facts["sha"], "dirty": facts["dirty"]}, "version": installed("deltasleuth"),
            "uv_lock_sha256": hashlib.sha256((release.ROOT / "uv.lock").read_bytes()).hexdigest(),
            "options": options, "environment": measured_on, "profile": _profile(baseline), "sizes": [],
            "notes": [],
        }  # fmt: skip
        _write(bench)
        for at, rows in enumerate(opts.rows):
            if found := _space(bench, rows, bench.constants):  # the re-check, with the measured constants
                for later in opts.rows[at:]:
                    why = (_space(bench, later, bench.constants) or found)[1]
                    bench.result["sizes"].append(_entry(later, why))
                bench.result["problems"].append(found[1])
                typer.echo(found[1], err=True)
                bench.code = 1
                break
            bench.result["sizes"].append(entry := _entry(rows))
            _size(bench, rows, entry)
            _measured(bench, entry)
    except KeyboardInterrupt:
        if bench.result:
            bench.result["problems"].append("interrupted")
        typer.echo("interrupted", err=True)
        bench.code = 130
    except _Stopped as exc:
        bench.result["problems"].append(str(exc))
        bench.code = 1
    except Exception as exc:  # a failed generation or connection: named in the file, then raised
        if bench.result:
            why = f"exit {exc.exit_code}" if isinstance(exc, typer.Exit) else type(exc).__name__
            bench.result["problems"].append(f"failed ({why})")
        raise
    finally:
        if bench.result:
            _write(bench)
    if bench.result:
        reasons, done = _unpublishable(bench.result), "complete" if bench.result["complete"] else "incomplete"
        judged = f"not publishable ({', '.join(reasons)})" if reasons else "publishable"
        typer.echo(f"wrote {bench.path}: {done}, {judged}")
    return bench.code


def _preflight(bench: Bench) -> None:
    """Stage 3: every requested size against the estimates, once the database is up, before any database."""
    for rows in bench.opts.rows:
        if found := _space(bench, rows, ESTIMATES):
            fail(found[0])


@app.command()
def run(
    out: Annotated[Path, typer.Option(help="The result file's folder (docs/bench).")],
    rows: Annotated[
        str, typer.Option(help="Rows a side: multiples of 10,000, k and M allowed.")
    ] = "100k,1M,10M",
    scenarios: Annotated[str, typer.Option(help="Of sparse, dense, clustered and graph.")] = ",".join(
        SCENARIOS
    ),
    shape: Annotated[str, typer.Option(help="narrow or wide: the table scenarios' columns.")] = "narrow",
    repeat: Annotated[int, typer.Option(min=1, help="Repetitions of each scenario at each size.")] = 3,
    server: ServerOpt = None,
    work: WorkOpt = None,
    timeout: Annotated[int, typer.Option(min=1, help="Minutes a repetition may take.")] = 240,
    stall: Annotated[int, typer.Option(min=1, help="Minutes without progress before an abort.")] = 10,
    max_rss: Annotated[
        int | None, typer.Option(min=1, help="ds's memory limit (default: 60% of it).")
    ] = None,
    allow_dirty: Annotated[
        bool, typer.Option("--allow-dirty", help="Run uncommitted code: unpublishable.")
    ] = False,
) -> None:
    """Measure: a throwaway postgres:16-alpine, or --server's, deterministic tables, ``ds run`` gated on the
    truth per repetition, one result file rewritten after each (docs/mvp/BENCHMARK.md §6)."""
    limits = Limits(timeout, stall, max_rss or int(0.6 * _memory()))
    opts = _refuse_arguments(out, rows, scenarios, shape, server, work, repeat=repeat, limits=limits,
                             allow_dirty=allow_dirty)  # fmt: skip
    env, facts = _refuse_environment(opts)
    bench_id, started = secrets.token_hex(4), datetime.now(UTC)
    folder = opts.parent.resolve() / f"dsbench-{bench_id}"
    db = Server(opts.server[1], bench_id) if opts.server else Container(bench_id, folder, env)
    path = opts.out / f"bench-{started:%Y%m%dT%H%M%SZ}-{facts['sha'][:12]}.json"
    bench = Bench(bench_id, db, folder, env, opts, path)
    # Unwind as Ctrl-C: a kill, a closed terminal or SSH session; an ignored hangup (nohup) stays ignored.
    unwound = [s for s in (signal.SIGTERM, signal.SIGHUP) if signal.getsignal(s) is not signal.SIG_IGN]
    previous, awake, code = {s: signal.signal(s, signal.default_int_handler) for s in unwound}, None, 130
    try:
        fsio.ensure_private_dir(folder)
        (folder / MARKER).write_text(f"{MARK} {bench_id}\n")
        if sys.platform == "darwin":  # no idle sleep while this process lives
            awake = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())], env=env)  # noqa: S603, S607
        db.start()
        _preflight(bench)
        code = _bench(bench, facts, started)
    except KeyboardInterrupt:
        typer.echo("interrupted", err=True)
    finally:
        try:
            db.remove()
        finally:  # even when a second signal cuts the removal short
            shutil.rmtree(folder, ignore_errors=True)
            if awake is not None:
                awake.terminate()
                awake.wait()
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    raise typer.Exit(code)


SECONDS = ("run", "first_progress", "scan", "l1", "l2", "reads_tail", "cycle_end", "process")
METRICS = (*(f"{name}_s" for name in SECONDS), "keys_per_s", "tail_keys_per_s", "rows_per_s",
           "peak_rss_bytes", "rss_scan_end_bytes", "ds_cpu_s", "database_cpu_s", "temp_bytes", "store_bytes",
           "evidence_bytes", "stored_per_key")  # fmt: skip
COLUMNS = {"run_s": "run s", "first_progress_s": "first progress s", "scan_s": "scan s", "l1_s": "L1 s",
           "l2_s": "L2 s", "reads_tail_s": "reads tail s", "cycle_end_s": "cycle end s",
           "keys_per_s": "keys/s", "tail_keys_per_s": "tail keys/s", "rows_per_s": "rows/s",
           "peak_rss_bytes": "peak MB", "ds_cpu_s": "ds CPU s", "database_cpu_s": "database CPU s",
           "temp_bytes": "temp MB", "stored_per_key": "stored B per differing key"}  # fmt: skip
MODEL = "T ≈ max(scan_s, first_progress_s + D / tail_keys_per_s) + cycle_end_s"


def _metrics(rep: dict[str, Any]) -> dict[str, float | None]:
    s, seen, stored, memory, cpu = rep["seconds"], rep["observed"], rep["stored"], rep["memory"], rep["cpu"]
    differing, kept = seen["candidates"], stored["store_bytes"] + stored["evidence_bytes"]
    return {f"{name}_s": s[name] for name in SECONDS} | {
        "keys_per_s": rep["reads"]["keys_per_s"], "tail_keys_per_s": rep["reads"]["tail_keys_per_s"],
        "rows_per_s": round(seen["rows_a"] / s["scan"], 1) if s["scan"] else None,
        "peak_rss_bytes": memory["peak_rss_bytes"], "rss_scan_end_bytes": memory["rss_scan_end_bytes"],
        "ds_cpu_s": cpu["ds_s"], "database_cpu_s": cpu["database_s"],
        "temp_bytes": sum(rep["sources"][side]["temp_bytes"] for side in "ab"),
        "store_bytes": stored["store_bytes"], "evidence_bytes": stored["evidence_bytes"],
        "stored_per_key": round(kept / differing) if differing else None,
    }  # fmt: skip


def _analysed(rep: dict[str, Any]) -> str | None:
    """The store's planner statistics over the cycle end, from each table's last analyses against
    finished_at, the cycle end's start: "analysed" when every table was analysed before it began, else which
    tables were "unanalysed" or "analysed during the cycle end"; None when not recorded. The findings' own
    ANALYZE during the cycle end, with no autovacuum analysis of them in it, is the product's, of the root
    causes it links (repo.replace_root_causes): it runs only on a table that has statistics, so they were
    analysed before the cycle end began."""
    found = rep.get("statistics") or {}
    if found.get("finished_s") is None or not found.get("tables"):
        return None
    end = (rep["seconds"]["run"] or 0) - found["finished_s"]  # the cycle end's length
    kinds: dict[str, list[str]] = {}
    for table, times in found["tables"].items():
        at = [s for s in times.values() if s is not None]
        linked = table == "findings" and [s for s in at if 0 < s <= end] == [times["analyze_s"]]
        during = "analysed during the cycle end" if any(s <= end for s in at) else "unanalysed"
        kind = "analysed" if linked or any(s <= 0 for s in at) else during
        kinds.setdefault(kind, []).append(table)
    if len(kinds) == 1:
        return next(iter(kinds))
    return "; ".join(f"{kind}: {', '.join(tables)}" for kind, tables in kinds.items())


def summary(repetitions: list[dict[str, Any]]) -> dict[str, Any]:
    """Per cache state, over the correct repetitions (D6): n, the store's statistics over the cycle end, each
    metric's median, min and max, and whether run_s spreads over 10% of its median (n ≥ 2). A restart's
    effect is its own state, never noise; so are other statistics, never pooled: "<cache>, <statistics>"."""
    groups: dict[tuple[str, str | None], list[dict[str, float | None]]] = {}
    for rep in repetitions:
        if rep["correct"]:
            groups.setdefault((rep["cache"], _analysed(rep)), []).append(_metrics(rep))
    split = Counter(cache for cache, _ in groups)
    out: dict[str, Any] = {}
    for (cache, analysed), rows in groups.items():
        name = cache if split[cache] == 1 else f"{cache}, {analysed}"
        out[name] = {"n": len(rows), "statistics": analysed}
        for metric in METRICS:
            if values := [row[metric] for row in rows if row[metric] is not None]:
                out[name][metric] = {
                    "median": statistics.median(values),
                    "min": min(values),
                    "max": max(values),
                }
        run_s = out[name].get("run_s")
        out[name]["noisy"] = (
            bool(run_s) and len(rows) >= 2 and run_s["max"] - run_s["min"] > run_s["median"] / 10
        )
    return out


def fit(xs: list[float], ys: list[float]) -> tuple[float, float, float | None] | None:
    """y ≈ a + b·x by least squares, with its largest relative residual (None with two points: linearity not
    tested); None with fewer than two."""
    if len(xs) < 2:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sum((x - mx) ** 2 for x in xs)
    a = my - b * mx
    residual = max((abs(a + b * x - y) / abs(y) for x, y in zip(xs, ys, strict=True) if y), default=0.0)
    return a, b, residual if len(xs) > 2 else None


def _headlines(record: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """The states a scenario's numbers lead with: warm (each of its statistics states, the most repetitions
    first), else the one state there is."""
    states = record["summary"]
    warm = sorted((k for k in states if k.partition(",")[0] == "warm"), key=lambda k: -states[k]["n"])
    return [(k, states[k]) for k in warm or list(states)[:1]]


def _headline(record: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The state a scenario's numbers lead with, and its statistics."""
    return next(iter(_headlines(record)), None)


def _points(result: dict[str, Any], scenario: str) -> list[tuple[int, int, dict[str, Any]]]:
    """(N, D, headline statistics) of each size at which ``scenario`` ran every repetition, each correct."""
    points = []
    for size in result["sizes"]:
        record = next((r for r in size["scenarios"] if r["scenario"] == scenario), None)
        reps = record["repetitions"] if record else []
        if len(reps) == result["options"]["repeat"] and all(rep["correct"] for rep in reps):
            if headline := _headline(record):  # type: ignore[arg-type]
                points.append((size["rows"], truth(scenario, size["rows"])["candidates"], headline[1]))
    return points


def _noisy(points: list[tuple[int, int, dict[str, Any]]]) -> str:
    """The sizes among ``points`` whose headline state is noisy."""
    return ", ".join(_label(n) for n, _, stats in points if stats.get("noisy"))


def _value(name: str, value: float) -> str:
    if name.endswith("_bytes"):
        return f"{value / 1e6:,.0f}"
    if name.endswith("per_s") or name == "stored_per_key":
        return f"{value:,.0f}"
    return f"{value:.2f}" if value < 10 else f"{value:.1f}"


def _cell(stats: dict[str, Any] | None, name: str) -> str:
    if not stats or name not in stats:
        return "-"
    m, n = stats[name], stats["n"]
    if n == 1:
        return _value(name, m["median"])
    return f"{_value(name, m['median'])} ({_value(name, m['min'])}–{_value(name, m['max'])}, n {n})"  # noqa: RUF001


def _conditions(path: Path, result: dict[str, Any]) -> str:
    """The file's conditions: under --server the hardware is the harness's host's, and the settings are
    stock only when they are the image's."""
    env, w = result["environment"], result["profile"]["settings"]["worker"]
    host, docker, pg = env["host"], env["docker"], env["postgres"]
    where, image = "a dedicated server", f" ({pg['image']})" if pg["image"] else ""
    if docker:
        where = (f"Docker {docker['version']} ({docker['os']}, {docker['cpus']} CPUs,"
                 f" {_gb(docker['memory_bytes'])} GB, cgroup v{docker['cgroup']})")  # fmt: skip
    hardware = (f"{host['model']} ({host['cpu']}, {host['cpus']} CPUs, {_gb(host['memory_bytes'])} GB,"
                f" {host['os']})")  # fmt: skip
    if env["topology"] != "container":
        hardware = f"the harness's host {hardware} (the database server's hardware is not recorded)"
    changed = [f"{name} {value}" for name, value in pg["settings"].items() if STOCK.get(name) != value]
    settings = f"non-stock settings ({', '.join(changed)})" if changed else "stock settings"
    return (f"`{path.name}` (commit {result['commit']['sha'][:12]}, sha256"
            f" {hashlib.sha256(path.read_bytes()).hexdigest()}): observed on {hardware}, synthetic data;"
            f" {where}; PostgreSQL {pg['version']}{image} with {settings} holding"
            f" both sources and the control plane; topology {env['topology']}, rtt {env['rtt_ms']} ms; worker"
            f" concurrency {w['concurrency']}, lease {w['lease_seconds']} s, poll {w['poll_seconds']} s (D4);"
            f" {result['options']['shape']} rows.")  # fmt: skip


def _flags(result: dict[str, Any], record: dict[str, Any], name: str, stats: dict[str, Any]) -> str:
    cpus, noise = result["environment"]["host"]["cpus"], [rep["noise"] for rep in record["repetitions"]]
    flags = {"noisy": stats.get("noisy"), "loaded": any(x["load_1m"] > cpus / 2 for x in noise),
             "busy": any((x["others"] or {}).get("cpu_pct", 0) >= 10 for x in noise)}  # fmt: skip
    if (analysed := stats.get("statistics")) not in (None, "analysed") and analysed not in name:
        flags[analysed] = True  # the store's statistics over the cycle end, when not all analysed before
    return ", ".join(flag for flag, held in flags.items() if held)


def _failure(result: dict[str, Any], rows: int, scenario: str) -> str | None:
    """Why ``scenario`` has no numbers at ``rows`` (C7: failures stay in), or None: the size's re-check, a
    repetition not correct, a failure at a smaller size, else what stopped the run."""
    sizes = {size["rows"]: size for size in result["sizes"]}
    if (size := sizes.get(rows)) and size["did_not_run"]:
        return size["did_not_run"]
    record = next((r for r in size["scenarios"] if r["scenario"] == scenario), None) if size else None
    reps = record["repetitions"] if record else []
    if failed := next((rep for rep in reps if not rep["correct"]), None):
        return failed["aborted"] or failed["problems"][0]
    if len(reps) == result["options"]["repeat"]:
        return None
    for smaller in (sizes[n] for n in sizes if n < rows):  # a scenario not correct skips its larger sizes
        earlier = next((r for r in smaller["scenarios"] if r["scenario"] == scenario), None)
        if earlier and not all(rep["correct"] for rep in earlier["repetitions"]):
            return "not run after a failure at a smaller size"
    return (result["problems"] or ["no cause recorded (the run was killed, or is still running)"])[-1]


def _table(shape: str, files: list[tuple[Path, dict[str, Any]]]) -> list[str]:
    """Per requested size and scenario of each file a row, marked with its file (DRAFT when unpublishable):
    each warm statistics state's headline medians, or why it did not complete."""
    widths = {name.split(".")[0]: f"{name.split('.')[0]}: {t['columns']} columns, {t['row_bytes']} B a row"
              for _, r in files for size in r["sizes"] for name, t in size["tables"].items()}  # fmt: skip
    apart = ["restarted/first run s", "restarted/first scan s"]
    header = ["File", "Scenario", "Rows a side", "D", "State", *COLUMNS.values(), *apart, "Flags"]
    out = ["", f"**{shape} rows** ({'; '.join(widths.values())}): headline medians (min–max, n)", "",  # noqa: RUF001
           "| " + " | ".join(header) + " |", "|" + "---|" * len(header)]  # fmt: skip
    for path, result in files:
        named = f"`{path.name}`" + (" (DRAFT)" if _unpublishable(result) else "")
        for rows in result["options"]["rows"]:
            size = next((s for s in result["sizes"] if s["rows"] == rows), None)
            for scenario in result["options"]["scenarios"]:
                cells = [named, f"`{scenario}`", _label(rows), f"{truth(scenario, rows)['candidates']:,}"]
                if why := _failure(result, rows, scenario):
                    out.append("| " + " | ".join([*cells, f"did not complete: {why}"]) + " |")
                    continue
                record = next(r for r in size["scenarios"] if r["scenario"] == scenario)  # type: ignore[index]
                for name, stats in _headlines(record):
                    warm = name.partition(",")[0] == "warm"
                    first = record["summary"].get("restarted") or record["summary"].get("first")
                    first = first if warm else None
                    row = [*cells, name if warm else f"{name} (only)", *(_cell(stats, k) for k in COLUMNS),
                           _cell(first, "run_s"), _cell(first, "scan_s"),
                           _flags(result, record, name, stats)]  # fmt: skip
                    out.append("| " + " | ".join(row) + " |")
    return out


def _fitted(points: list[tuple[int, int, dict[str, Any]]], name: str, by: str) -> Any:
    chosen = [(n if by == "N" else d, stats[name]["median"]) for n, d, stats in points if name in stats]
    return fit([x for x, _ in chosen], [y for _, y in chosen])


def _fits(path: Path, result: dict[str, Any]) -> list[str]:
    """Per scenario each fit, marked where it uses a noisy size; run_s's exponent unless either of its two
    sizes is noisy."""
    out = []
    for scenario in result["options"]["scenarios"]:
        if len(points := _points(result, scenario)) < 2:
            out.append(f"- `{path.name}` `{scenario}`: {len(points)} size measured: no fit")
            continue
        texts = []
        for name, by in (("run_s", "N"), ("scan_s", "N"), ("first_progress_s", "N"), ("cycle_end_s", "D"),
                         ("peak_rss_bytes", "D")):  # fmt: skip
            if (found := _fitted(points, name, by)) is None:
                texts.append(f"{name}: no fit")
                continue
            a, b, residual = found
            tested = (
                "linearity not tested" if residual is None else f"largest relative residual {residual:.0%}"
            )
            noisy = _noisy([p for p in points if name in p[2]])
            texts.append(f"{name} ≈ {a:.4g} + {b:.4g}·{by} ({tested}{f'; noisy at {noisy}' * bool(noisy)})")
        (n1, _, s1), (n2, _, s2) = points[-2:]
        if noisy := _noisy(points[-2:]):
            texts.append(f"run_s's exponent between {_label(n1)} and {_label(n2)}: none, noisy at {noisy}")
        else:
            exponent = math.log(s2["run_s"]["median"] / s1["run_s"]["median"]) / math.log(n2 / n1)
            texts.append(f"run_s's exponent between {_label(n1)} and {_label(n2)}: {exponent:.2f}")
        out.append(f"- `{path.name}` `{scenario}`: " + "; ".join(texts))
    return out


def _model(scenario: str, n: int, fits: dict[str, Any], rate: float | None) -> float:
    """The planning model's time at ``n`` rows a side."""

    def term(name: str, x: float) -> float:
        return fits[name][0] + fits[name][1] * x

    d = truth(scenario, n)["candidates"]
    return max(term("scan_s", n), term("first_progress_s", n) + (d / rate if rate else 0)) + term(
        "cycle_end_s", d
    )


def _extrapolated(files: list[tuple[Path, dict[str, Any]]], rows: int) -> list[str]:
    """The planning model, labelled: each term from its fit, the tail rate from the largest size; its error
    at each measured size, a noisy one marked; at ``rows`` its time beside the extrapolated peak RSS, never
    past the memory or 10 times the scenario's largest correct size (D7), marked when a fit uses a noisy
    size."""
    out = ["", "**Extrapolated, not measured**", "",
           f"{MODEL}: each term from its fit, D the differing keys, the tail rate from the largest"
           " size."]  # fmt: skip
    terms = (("scan_s", "N"), ("first_progress_s", "N"), ("cycle_end_s", "D"), ("peak_rss_bytes", "D"))
    for path, result in files:
        memory = result["environment"]["host"]["memory_bytes"]
        for scenario in result["options"]["scenarios"]:
            points, head = _points(result, scenario), f"- `{path.name}` `{scenario}`"
            fits = {name: _fitted(points, name, by) for name, by in terms}
            if len(points) < 2 or None in fits.values():
                out.append(f"{head}: fewer than two sizes measured: not extrapolated")
                continue
            if rows > 10 * (largest := points[-1][0]):
                out.append(f"{head}: more than 10 times its largest measured size ({_label(largest)}): not"
                           " extrapolated")  # fmt: skip
                continue
            rate = points[-1][2].get("tail_keys_per_s", {}).get("median")
            errors = ", ".join(f"{_label(n)} {round(_model(scenario, n, fits, rate) / s['run_s']['median'] - 1, 2) + 0:+.0%}"  # noqa: E501
                               f"{' (noisy)' * bool(s.get('noisy'))}" for n, _, s in points)  # fmt: skip
            rss = fits["peak_rss_bytes"][0] + fits["peak_rss_bytes"][1] * truth(scenario, rows)["candidates"]
            if rss > memory:
                at = f"would not fit in {_gb(memory)} GB: not extrapolated"
            else:
                unmeasured = "" if rate else " (tail rate not measured: under 2,000 keys)"
                time_s = _model(scenario, rows, fits, rate)
                at = f"T ≈ {time_s:,.0f} s beside peak RSS ≈ {_gb(rss)} GB{unmeasured}"
            noisy = f"; its fits use noisy sizes ({_noisy(points)})" * bool(_noisy(points))
            out.append(f"{head}: the model's error at {errors}; at {_label(rows)}: {at}{noisy}")
    return out


def render(files: list[tuple[Path, dict[str, Any]]], extrapolate: int | None = None) -> str:
    """§8's Markdown: each file's conditions, per shape the headline medians, per file and scenario the fits,
    the extrapolation only under its heading, and the caveat block."""
    out = []
    for path, result in files:
        if reasons := _unpublishable(result):
            out.append(f"**DRAFT: `{path.name}` is not publishable ({', '.join(reasons)}).**")
        out.append(_conditions(path, result))
    for shape in dict.fromkeys(result["options"]["shape"] for _, result in files):
        out += _table(shape, [(p, r) for p, r in files if r["options"]["shape"] == shape])
    out += ["", "**Fits** by least squares over one file's sizes, N rows a side and D differing keys:", ""]
    for path, result in files:
        out += _fits(path, result)
    if extrapolate:
        out += _extrapolated(files, extrapolate)
    return "\n".join([*out, "", NOT_MEASURED, "", CAVEAT]) + "\n"


def _load(path: Path) -> dict[str, Any]:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        result = None
    if not isinstance(result, dict) or result.get("format") != FORMAT:
        fail(f"{path} is not a {FORMAT} result")
    return result


@app.command()
def report(
    files: Annotated[list[Path], typer.Argument(help="Result files (bench-….json).")],
    draft: Annotated[bool, typer.Option("--draft", help="Render unpublishable files, marked DRAFT.")] = False,
    extrapolate: Annotated[
        str | None, typer.Option(help="Rows a side to extrapolate to (at most 10x).")
    ] = None,
) -> None:
    """Print the files' numbers as Markdown under the claim rules (docs/mvp/BENCHMARK.md §8)."""
    loaded = [(path, _load(path)) for path in files]
    for path, result in loaded:
        if (reasons := _unpublishable(result)) and not draft:
            fail(f"{path} is not publishable ({', '.join(reasons)}): pass --draft to render it marked DRAFT")
    rows = sizes(extrapolate, "--extrapolate")[0] if extrapolate else None
    for path, result in loaded:
        largest = max((s["rows"] for s in result["sizes"] if not s["did_not_run"]), default=0)
        if rows and rows > 10 * largest:
            fail(f"--extrapolate {extrapolate} is more than 10 times the largest size measured in {path}"
                 f" ({_label(largest) if largest else 'none'})")  # fmt: skip
    env, host = _env(), ("os", "model", "cpu", "cpus", "memory_bytes")
    for path, result in loaded:
        sha = result["commit"]["sha"]
        if _run(
            ["git", "diff", "--quiet", sha, "--", *DIRTY], check=False, cwd=release.ROOT, env=env
        ).returncode:
            typer.echo(f"warning: {path} was measured at {sha[:12]}; deltasleuth/, tools/, pyproject.toml or"
                       " uv.lock changed since", err=True)  # fmt: skip
    hosts = {json.dumps([r["environment"]["host"][k] for k in host]) for _, r in loaded}
    for what, found in (("hosts", hosts), ("commits", {r["commit"]["sha"] for _, r in loaded})):
        if len(found) > 1:
            typer.echo(
                f"warning: these files come from {len(found)} {what}: their numbers are not one curve",
                err=True,
            )
    typer.echo(render(loaded, rows), nl=False)


@app.command()
def clean(server: ServerOpt = None, work: WorkOpt = None) -> None:
    """After a kill: remove the labelled containers with their volumes, or drop --server's marked databases,
    and remove the marked dsbench-* work folders under --work. A live run's go too."""
    containers = databases = 0
    if server:
        databases = _drop_marked(_admin(server))
    elif shutil.which("docker"):
        env = _env(_endpoint())
        if ids := _run(["docker", "ps", "-aq", "--filter", f"label={LABEL}"], env=env).stdout.split():
            _run(["docker", "rm", "-f", "-v", *ids], env=env)
            containers = len(ids)
    parent = work or Path(tempfile.gettempdir())
    folders = [f for f in parent.glob("dsbench-*") if f.is_dir() and (f / MARKER).is_file()]
    for folder in folders:
        shutil.rmtree(folder)
    typer.echo(
        f"removed {containers} container(s), {databases} database(s) and {len(folders)} work folder(s)"
    )


if __name__ == "__main__":
    app()
