# Benchmark harness: contract

This contract is binding. It builds the benchmark harness
as a maintainer's tool beside `tools/dsv.py` (LICENCE §14) and `tools/release.py` (RELEASE §9), and amends the
documents in §14.

**Goal:** published numbers beyond 1M rows a side: measured here at 0.1M, 1M and 10M with a checked fit,
100M one command on a server. **Shape:** `tools/bench.py run` starts a throwaway
`postgres:16-alpine` (never pulled) or takes a dedicated server, generates deterministic tables in it with
`generate_series`, runs the product's own `ds run` on a fresh control plane per repetition, gates each run
on exact ground truth, measures time, memory, CPU, source load and storage, and rewrites one result file
(`deltasleuth-bench/1`) after every repetition; `report` renders doc tables; `clean` recovers from a kill.
Under `deltasleuth/` only §16's statistics finding is fixed: a cycle end analyses a store's tables when one
holds rows but no statistics (`repo.analyse_new`), and a page of `repo.list_findings` seeks its first key by
index.
Every number is *observed on stated hardware, synthetic data*.

## 1. Decisions (each one seam)

- **D1** A maintainer's tool, not `ds bench`: it starts containers and drops databases, which nothing installed
  beside a site's store should do. `release.SHIPPED` and `deploy/Dockerfile.dockerignore` exclude `tools/`.
- **D2** It drives the product only through `ds init`, `ds migrate` and `ds run` (the checkout's
  `Path(sys.executable).with_name("ds")`) and reads results with read-only SQL on its throwaway store,
  unsealed columns only.
- **D3** One PostgreSQL server holds both sources and the control plane, as behind TABLES §7's 6.8 s; stock
  image settings.
- **D4** Worker `{concurrency: 4, lease_seconds: 120, poll_seconds: 0.05}`: the shipped concurrency and
  lease, the DS_PERF poll, so it measures work rather than idle polling; every report says so (§10).
- **D5** `sparse` at 1M holds the DS_PERF table test's rows, not its profile (§7's notes list the
  differences); `graph` is `test_graph.py`'s generator scaled, with `graph_spec` (`maxComponent: 20`).
- **D6** Correctness gates speed: a repetition counts only if its counters equal the ground truth.
- **D7** Extrapolation, the planning model included, exists only under `report --extrapolate`, at most 10×
  the largest measured size and never past the host's memory (§8); it is never a measurement (C1).
- **D8** The container's superuser gets a random password from a file, never trust on a published port,
  which every local user of a shared server could reach.
- **D9** One invocation at a time (B11).
- **D10** Result files go to `docs/bench/`, outside `SHIPPED`; the shipped BENCHMARK.md
  cites each by name, commit and sha256.

## 2. What is measured, and what is not

| Scenario | Differing keys (N rows a side) | Measures |
|---|---|---|
| `sparse` | ⌊N/100,000⌋ + 1 | the hash scan: sort, stream, merge |
| `dense` | N/100, every 100th key | batched per-key reads, cycle end, memory and storage per differing key |
| `clustered` | N/100, in 10 contiguous blocks | the same count in bursts of adjacent keys |
| `graph` | 2·N/10,000 (N facts a side) | L1 and L2 at scale |

`--shape narrow` (default) or `wide` applies to the table scenarios.

| Metric | Definition | Clock |
|---|---|---|
| `run_s` (headline) | `clock_timestamp()` read by the poll that first sees a terminal status, minus `ds.runs.created_at` (create_run's insert): scan, reads and the whole cycle end | control plane |
| `first_progress_s` | t_progress − `first_at`: both sorts (the scan's first fetch of side a's cursor, then of side b's) | DeltaSleuth host |
| `scan_s` | t_done − `first_at`: also each source's snapshot hold | host |
| `l1_s`, `l2_s` | `graph_at − first_at`; t_done − `graph_at` | host |
| `reads_tail_s`, `cycle_end_s` | t_drained − t_done; t_end − t_drained | host |
| `keys_per_s` | side a's first-pass observations ÷ (max − min `observed_at`), one 200-key group short | host |
| `tail_keys_per_s` | keys open at t_done ÷ `reads_tail_s` | host |
| `rows_per_s` | `rows_a ÷ scan_s`, always shown with row bytes and columns | host |
| memory | `ru_maxrss` from `os.wait4` (bytes on darwin, KiB on Linux, normalised); `ps -o rss=` at t_done | kernel |
| CPU | the child's `ru_utime + ru_stime`; the container's cgroup v2 `cpu.stat usage_usec` delta | kernel |
| `sources.a`, `.b` | `pg_stat_database` deltas (`temp_files`, `temp_bytes`, `blks_read`, `blks_hit`, `tup_returned`) 1 s after exit | server |
| `tables` | `pg_table_size`, `pg_indexes_size`, `row_bytes` (avg `pg_column_size(t.*)`, keys ≤ 10,000; `links`, keyed by text: its first 10,000 rows), `generated_s` (never a claim) | server |
| `stored` | store growth over its template; evidence bytes and files | server, disk |

t_* are the harness's poll times every 0.1 s (±0.1 s): t_progress first sees `checkpoint ≥ 1` or `done`;
t_done `done` (and one `count(*)` of open keys); t_drained no open key (`EXISTS` over `run_keys_open_idx`);
t_end a terminal status. `ds.runs.finished_at` enters no metric: it is stamped with the start of the
ending transaction, before the cycle end's work (§16). Like the DS_PERF wall times, `run_s` includes the
cycle end. Rates under 2,000 keys are null.

**The harness and its results are PostgreSQL's:** both sources and the store are PostgreSQL, and no figure
here applies to a MySQL or Oracle side; those sides' own measurements, each run once by `DS_PERF` tests,
are in [TABLES.md](TABLES.md) §13.9. Nor does any apply to a side read from CSV or Parquet files
([FILES.md](FILES.md)): those sides' measurements, single runs of `DS_PERF` tests too, are in TABLES
§14.9.

**Not measured, said in every report:** the service crawl and mixed pairs, rate-bound at
`T = N·(1+m·t)/r` (architecture §12.1); remote sources beyond `rtt_ms` (the median of 200 `SELECT 1`); a
cold cache; soak re-diffs; concurrent runs; other engines; scale-out; the release image; the accuracy draw
and the readiness measurement (§15).

**Table pre-checks and the schema guard** are not this harness's: no scenario runs a pre-check, and
the schema guard's look at each scan pass (V2, one catalogue statement a side on the pass's own sessions)
is inside every `scan_s` and `run_s` measured since the guard was built and in none before it. Their own figures, each run
once by `DS_PERF` tests in `tests/e2e/test_precheck.py` and `test_drift.py`, are in
[PRECHECKS.md](PRECHECKS.md) §12; the two `DS_PERF` table tests re-run there with the guard took 7.1 s (6.8 s
before) and 42.2 s (43 s before).

## 3. Where it runs

**Container mode (default)**, verbatim:

```
docker run -d --pull never --name dsbench-<id> --label deltasleuth.bench=<id> -p 127.0.0.1:<port>:5432
  --shm-size 1g -v <work>/pg_password:/run/secrets/pg_password:ro
  -e POSTGRES_PASSWORD_FILE=/run/secrets/pg_password postgres:16-alpine
```

`<id>` is `secrets.token_hex(4)`; `<port>` a free loopback port (found as `tests/conftest.py`'s
`_free_port` does), fixed across restarts. The Docker endpoint is resolved once with the caller's
environment (`docker context inspect --format '{{.Endpoints.docker.Host}}'`) and must be `unix://` or
`npipe://` (B1): the port and the password file must be on this machine. The password
(`secrets.token_urlsafe(24)`, a 0600 file) is read by the entrypoint as root (`file_env` precedes `gosu
postgres`): never in an argv, `docker inspect` or the environment of the harness or its children. `file_env`
exports it, though, and the entrypoint unsets `POSTGRES_PASSWORD_FILE` and `PGPASSWORD` but not it, so the
postmaster keeps `POSTGRES_PASSWORD` in its environment while the container runs: root or uid 70 (the image's
`postgres`) where the container runs (Docker Desktop's VM on darwin) can read it, and so can `docker exec`. The
harness leaves that copy as it is; D8's concern, other local users reaching the port, is unaffected. DSNs
(host `127.0.0.1`, `sslmode=disable` for this loopback container only, and `hostaddr=127.0.0.1`) sit in 0600
files, read as
`dsn_ref: file:<absolute path>`; the image is checked with `docker image inspect` (B2). Before each
scenario's first repetition at a size the server restarts (`docker restart -t 300`, then `pg_isready`); the restart is clean when
`pg_postmaster_start_time()` advanced and `docker logs --since` lacks `database system was not properly
shut down` since the restart, else the repetition carries the problem `restart was not clean (crash recovery)`. `finally`
runs `docker rm -f -v` (the image declares a volume), then removes the work folder even when a second signal
cuts the removal short; SIGTERM and SIGHUP (a closed terminal or SSH session) unwind as Ctrl-C, as SIGTERM
does in `tests/conftest.py`, but a hangup already ignored (`nohup`) stays ignored, so the run goes on.

**Server mode** (`--server file:/abs/admin-dsn`: `dsv.file_ref`, `read_secret`), for a dedicated PostgreSQL
16: refused unless its role is a superuser (B10) and it holds no database but `postgres`, the templates and
marked benchmark databases (B9, B11). Each DSN is `make_conninfo(admin, dbname=…)`, so the admin DSN's TLS
parameters are kept; a host that is not loopback or a socket path needs `sslmode=verify-full` (B14). The
admin DSN names its host or hostaddr (a socket folder counts) and no `service`, and it names a hostaddr while
`PGHOSTADDR` or `PGSERVICE` is set, and a port, user, password and `sslmode` while `PGPORT`, `PGUSER`,
`PGPASSWORD` or `PGSSLMODE` is: libpq fills what a DSN omits from the caller's environment for the
harness's own connections, which `ENV` does not reach, so B14 judges the host they use. No
restart (cache `first`), no container CPU, database disk `not checked`. Both modes create only
`dsbench_<id>_a`, `_b`, `_store` and `_run`, each `COMMENT ON DATABASE … IS 'deltasleuth-bench <id>'`, and
drop only marked ones.

**Isolation:** the harness never changes `os.environ`. It builds one `ENV` (the caller's, without `PG*`,
`DS_*`, `DOCKER_HOST` and `DOCKER_CONTEXT`, plus the resolved endpoint as `DOCKER_HOST` in container mode)
and passes it to every child: `docker`, `git`, `ps` and each `ds` (only the one `docker context inspect` that
resolves the endpoint also gets the caller's `DOCKER_HOST` and `DOCKER_CONTEXT`). Every `ds` gets `--config`; the settings
declare no `auth`, `encryption`, LLM, `packs` or notification destination and the targets are its
databases. In container mode nothing leaves the host; with a remote server only the synthetic tables' and
the throwaway store's traffic does, over verified TLS (`topology: remote`, `rtt_ms`). It never touches a
container it did not start (an installation's own `deltasleuth-db-1` included) and records the others' count and CPU,
never their names.

**Work folder:** `<parent>/dsbench-<id>` (`--work` names the parent, default `tempfile.gettempdir()`;
B4 in a git tree or under `release.SYNCED`), 0700 by `fsio.ensure_private_dir`, with a `.deltasleuth-bench`
marker: password, DSNs (under `--server`, the admin's), settings, specs, workspace keys, evidence and `ds`
logs; removed at the end, or by `clean` after a kill.

**Preflight:** a size needs 1.5 × (tables held at once + 2 × `TEMP_BYTES` × N + `KEY_BYTES` per differing
key) on the database volume (`docker exec … df -Pk /var/lib/postgresql/data`) and the evidence estimate in
the work folder (`shutil.disk_usage`). On darwin Docker Desktop's disk image grows on the disk holding its
data folder (`~/Library/Containers/com.docker.docker/Data`), which needs the database volume's bytes too:
with the evidence's when the work folder is on that disk or there is no such folder. Under `--server` only
the work folder is checked. Every
requested size is checked with the estimates before any database exists (B12); each size is checked again,
before its generation, with constants measured at the smaller sizes. On darwin `caffeinate -i -w <pid>` runs; `pmset -g batt` gives the power source.
As built, the estimates (bytes) are 110 a narrow row with its index, 450 a wide one, 150 a link fact with both
indexes; `TEMP_BYTES`, the sort temp held at once a row a side: 80 a `users` row, whose sorts are held until the
scan ends, and 150 a fact, since a link fact's L1 sorts, whose files go when L1's snapshots close, end before
its L2 sorts begin; `KEY_BYTES` 5,000 a differing key in the store; and 12,000 a differing key of evidence (its
bytes plus a 4 KiB block per file). This Mac measured 107, 121, about 3,800 and 7,700 at 1M, and 435 a wide row
at 20k; 107, 121, 3,650 and 7,690 at 10M, and 443 a wide row at 1M; its sorts wrote 61 and 211 bytes a row a
side at 1M, 63 and 201 at 10M, of which the server held at most 61 and 121 at once at 1M, 63 and 107 at 10M
(Results). The tables of the requested scenarios' largest pair, and the larger temp, count. A re-check takes the
smaller size's bytes a row of each table, the store and evidence per differing key of its repetition with the
most, and each table's `temp_peak_bytes` a row a side (the most temp file bytes the watch's polls saw on the
server at once) only once a sort spilled: a size whose sorts fit in `work_mem` measures none.

## 4. Data (deterministic, generated in the server)

Logged tables (an unclean restart would truncate unlogged ones), each filled by one `INSERT … SELECT …
FROM generate_series(1::bigint, N) g` in a session with `synchronous_commit = off` and
`maintenance_work_mem = '1GB'`, then its primary key, `VACUUM (FREEZE, ANALYZE)` and `CHECKPOINT`, so no
measured read writes hint bits. Sides generate on two connections; `count(*)` must equal the truth. Per
size: `users` a; per table scenario `users` b, its repetitions, `DROP`; `users` a dropped; `links` a and b
likewise: one pair at a time. Sizes: multiples of 10,000, up to 10⁹.

**Narrow** (DS_PERF's): `users (userid bigint, email text, name text, amount numeric(12,2))` =
`(g, 'user' || g || '@example.com', 'Name ' || g, g * 1.25)`. **Wide** adds, identical on both sides,
`t1…t8 text` = `md5((g*k)::text)`, `n1…n4 numeric(14,2)` = `g*k/100.0`, `i1…i4 integer` =
`(g*k) % 2147483647`, `d1, d2 date` = `date '2000-01-01' + ((g*k) % 9000)::int`, `b1, b2 boolean` =
`(g+k) % 2 = 0` (k the column's number): 24 columns; g is bigint, so `g*k` holds at 10⁹.

| Pattern | Selected | Kind |
|---|---|---|
| `sparse` | `g % 100000 = 0 OR g = 3` | 0 if g = 3, else 2 |
| `dense` | `g % 100 = 50` | `(g / 100) % 10` |
| `clustered` | `(g − 1) % (N / 10) < N / 1000` | `g % 10` |

Side b by kind: 0 absent; 1 identical, plus key N + g's plain row; 2–5 `upper(email)`; 6–7 `name || ' Jr'`;
8–9 `amount + 0.01`. **Truth** (`truth()`, S selected): `sparse` MISMATCH ⌊N/100,000⌋, NOT_FOUND_B 1,
NOT_FOUND_A 0, `rows_b` N − 1; `dense` and `clustered` MISMATCH 0.8S, NOT_FOUND_B 0.1S, NOT_FOUND_A 0.1S,
`rows_b` N; all `rows_a` N, candidates S, matched N − S + NOT_FOUND_A, skipped 0, MATCH = matched, other
outcomes 0. At 1M `sparse` has the DS_PERF test's counts: 10/1/0.

**Graph** (E = N/2): side a `links (entity_code, id_type, id_value)` holds `('e'||g, 'phone', 'p'||g)` and
`('e'||g, 'email', 'x'||(g/2))`; side b moves each g with `g % 5000 = 2500` to `'x'||(g/2 + 1)`, a group side
a holds (M = E/5,000). Primary key on all three columns, and GRAPH §5's index on `(id_type, id_value)`.
Truth: `rows_a` = `rows_b` = N, NOT_FOUND_A = NOT_FOUND_B = M, candidates 2M, matched N − M; `scan.graph`
`groups_a` = `groups_b` = 3E/2 + 1, `groups_different` 2M, `groups_equal` 3E/2 + 1 − 2M, `rearrange` M,
`split` = `merge` = `oversized` = 0 (1M: 200 and 100, as today).

## 5. The profile (recorded verbatim in each result)

- **Settings:** `database_dsn_ref: file:<work>/dsn_run`, `evidence_dir: <work>/evidence` (emptied after each
  repetition), `workspace_dir: <work>/workspace`, `targets_file`, `worker` as D4; developer mode still seals
  evidence under the workspace keyring (RETENTION §1).
- **Targets** `db-a`, `db-b`: `{dsn_ref: file:…, rate_per_s: 1000, max_concurrency: 4}`, a token per
  200-key group per side: never binding below 200,000 keys/s.
- **Specs** (`table_spec`'s shape, sha256 recorded): `bench-tables`, sides `{name: users, keyColumns:
  [userid]}`, key `class: identifier` (the scan masks only candidate keys), columns omitted, `table_spec`'s
  `recheck` and `security`, `execution {concurrency: 8}`; `bench-graph` = `graph_spec`. Each store runs
  one pair and needs no licence: an open build enforces none (LICENCE §19). The results published
  before that section's decision were measured as an unlicensed evaluation of one pair, as a vendor
  build's store without a licence still is (LICENCE O1: no throughput fence).
- `READ_BATCH`, `scan.BATCH` and `ds migrate`'s peak RSS (`baseline_rss_bytes`).

## 6. One repetition

Once: `ds init`, then `ds migrate` with a settings file naming `dsbench_<id>_store`. Per size (smallest
first): preflight, generate. Per scenario and repetition (`--repeat`, default 3):

1. **Quiet:** `CHECKPOINT`; wait up to 60 s for no other active client backend or autovacuum worker; record
   the wait, `os.getloadavg()`, power, other containers' CPU (`docker stats --no-stream`; null under
   `--server`).
2. `CREATE DATABASE dsbench_<id>_run TEMPLATE dsbench_<id>_store`, marked; statistics before.
3. `Popen([ds, "run", spec, "--config", settings], env=ENV)`, output to files. Every 0.1 s one query
   (status, `done`, `checkpoint`, whether a key is open, whether a source backend is active,
   `clock_timestamp() − created_at`, the temp file bytes the server holds: `pg_ls_tmpdir()`) and
   `os.wait4(pid, WNOHANG)`; each second that query counts the open
   keys (the stall's `no fewer open keys`), and `ps` and new stderr. `first_at` and `graph_at`, set once, are
   read from the scan state after the run.
4. **Aborts** (SIGINT, then SIGKILL after 30 s):
   - `scan of run … paused: <Class>`, the first time (worker `_scan` retries every `OperationalError` but
     `ProgramLimitExceeded`, `temp_file_limit` and a full disk included, every `lease_seconds` without end;
     DEPLOY §7 documents the line);
   - any other worker warning ending in an `OperationalError` subclass's name (`keys of run … not saved`,
     `worker slot`, `sweep failed`, `draw of run … postponed`: `worker._handle`, `slot` and `sweep` retry
     them without end), the third time with the same text;
   - **stall:** until t_drained, no new checkpoint, no fewer open keys and no status change for `--stall`
     minutes (default 10); until t_done an active source backend (a sort, or L2's lookups) also counts as
     progress;
   - RSS over `--max-rss` (default 60% of memory); `--timeout` (240 min);
   - the harness's poll connection lost and not regained on one retry: `docker inspect`'s
     `State.OOMKilled`, `.Running` and `.ExitCode` name the cause (`unreachable` under `--server`).
5. **After:** `os.wait4` reaped the child, so the harness sets `proc.returncode =
   os.waitstatus_to_exitcode(status)` at once (`Popen.__del__` would otherwise `waitpid` a reused pid);
   settle 1 s; read deltas, run row, observation span, sizes, evidence, and each store table's
   (`run_keys`, `results`, `findings`, `observations`) `last_analyze` and `last_autoanalyze` from
   `pg_stat_user_tables` against `ds.runs.finished_at`, the cycle end's start; drop `_run`; rewrite the file
   with `fsio.atomic_write`.
6. **Judge:** correct only with exit 0, status `completed`, §4's truth in the scan state, `by_outcome` and
   `scan.graph`, every `counters.reconciliation[*].holds`, `snapshots_a` = `snapshots_b` = 1, no abort, and
   wall minus monotonic time under 5 s (else `host slept`).

A repetition not correct skips its scenario's remaining repetitions and larger sizes; the run exits 1. A
failed re-check before a size (§3) is the abort `did not run: …`: that size and the larger ones are not
run. Cache: `restarted` (server restarted, shared buffers empty, OS page cache warm: the Docker VM's or
the host's), then `warm`; `first` under `--server`. Each size records `fits_in_memory` against Docker's
`MemTotal`.

## 7. The result file (`deltasleuth-bench/1`)

`<out>/bench-<UTC yyyymmddThhmmssZ>-<sha12>.json`, `complete: false` until every requested size, scenario
and repetition ran. Counts, timings, sizes and environment only: no key, value, DSN, password, host or user
name, or path (the profile writes the work folder as `<work>`). `fits_in_memory` is null against a remote
server.

```
format, bench_id, complete, started, ended, publishable, problems[], commit{sha, dirty}, version, uv_lock_sha256
options{rows[], scenarios[], shape, repeat, server (boolean), timeout_min, stall_min, max_rss_bytes}
environment{host{os, model, cpu, cpus, memory_bytes, python, psycopg, libpq}, docker{version, cpus,
  memory_bytes, cgroup, os}|null, postgres{image, image_id, version, settings{shared_buffers, work_mem,
  maintenance_work_mem, max_wal_size, synchronous_commit, fsync, jit, max_parallel_workers_per_gather,
  temp_file_limit}}, topology (container|loopback|remote), rtt_ms}
profile{§5}
sizes[]{rows, fits_in_memory, did_not_run, tables{…}, scenarios[]{scenario, truth{}, repetitions[]{n, cache,
  restart_clean, correct, problems[], aborted, exit, status, observed{}, seconds{run, first_progress, scan,
  l1, l2, reads_tail, cycle_end, process}, reads{keys, span_s, keys_per_s, open_at_scan_end,
  tail_keys_per_s}, memory{peak_rss_bytes, rss_scan_end_bytes}, cpu{ds_s, database_s}, sources{a, b},
  temp_peak_bytes, stored{…}, statistics{finished_s, tables{<table>{analyze_s, autoanalyze_s}}},
  noise{quiet_wait_s, load_1m, power, others{containers, cpu_pct}, slept_s}},
  summary{<state>{n, statistics, <metric>{median, min, max}, noisy}}}}, notes[]
```

`dirty` is any line from `git status --porcelain --untracked-files=all -- deltasleuth tools pyproject.toml
uv.lock`, untracked files included. **Publishable** only when complete, clean, every repetition correct,
and none on battery, slept or after an unclean restart. `temp_peak_bytes` is the most temp file bytes the
polls saw held at once; `statistics.finished_s` is `finished_at − created_at`, and each `analyze_s` and
`autoanalyze_s` a table's last analysis minus `finished_at` (negative: before the cycle end began; null:
never). Summaries are per cache state and per statistics state, never pooled: `analysed` when every store
table was analysed before the cycle end began, else which were `unanalysed` or `analysed during the cycle
end`; a cache state whose repetitions differ in it splits into `<cache>, <statistics>` states. The
findings' own `analyze_s` during the cycle end, with no `autoanalyze_s` in it, counts as `analysed`: it is
the product's analysis of the root causes the cycle end links (`repo.replace_root_causes`,
[ACCURACY.md](ACCURACY.md) §3), which runs only on a table that has statistics. `warm` (n =
repeat − 1) is the headline and `restarted` or `first` is shown apart; with `--repeat 1` the one state is
shown, labelled. `noisy` (the state's spread of `run_s` over
10% of its median, n ≥ 2), a load average over half the CPUs and busy other containers are flagged beside
the numbers, never blocking. `notes` are automatic: D4; darwin's `os.fsync` without `F_FULLFSYNC`; the port
forward; `sources fit in the page cache: reads are memory reads`; `restarted`'s meaning; and D5's
differences from the DS_PERF table test (key class `identifier`, not `business_code`; `execution.concurrency:
8`, not the spec default; lease 120 s, not 30 s; side b generated, not UPDATEd and DELETEd, so no dead
tuples; frozen tables).

## 8. `report` and the claim rules

`report FILE… [--draft] [--extrapolate ROWS]` prints Markdown: a conditions line (`with stock settings`
only when the recorded settings are the image's, else `with non-stock settings (<setting> <value>, …)`; under
`--server` the hardware is `the harness's host <hardware> (the database server's hardware is not recorded)`);
per shape a table of §2's warm medians (min–max, n) per file, requested size and scenario, each row naming its
file (`(DRAFT)` when not publishable) and each warm statistics state its own row, `restarted` or `first` in
their own columns, with stored bytes per differing key, and `did not complete: <reason>` for a scenario that
did not run all its repetitions (a repetition's abort or problem, the size's re-check, `not run after a failure
at a smaller size`, else the problem that stopped the run: `interrupted`, the stopped database, `failed
(<Class>)`, or `no cause recorded (the run was killed, or is still running)`); per scenario, fits from one file
only, by least squares over the sizes where it ran every repetition correctly: `run_s`, `scan_s` and
`first_progress_s` against N, `cycle_end_s` and peak RSS against differing keys D, each with its largest
relative residual (`linearity not tested` with two sizes) and `noisy at <sizes>` when it uses a noisy size,
and `run_s`'s exponent between the two largest sizes (`none, noisy at <sizes>` when either is noisy); and
§10's caveat block. Only `--extrapolate` adds a block headed `Extrapolated, not measured`: the planning model
`T ≈ max(scan_s, first_progress_s + D / tail_keys_per_s) + cycle_end_s`, each term from its fit and the tail
rate from the largest size; the model's error against each measured size's warm `run_s`, `(noisy)` beside a
noisy one; and at ROWS each scenario's T beside its extrapolated peak RSS, `; its fits use noisy sizes
(<sizes>)` when they do, or `would not fit in <memory>: not extrapolated` where that RSS exceeds the host's
memory, or `more than 10 times its largest measured size (<rows>): not extrapolated` past 10 times the
scenario's largest correct size.

**Claims (binding on every document):** C1 a size is measured only when a committed publishable file
holds it: 100M stays `not yet measured`. C2 median with min–max, never a best run. C3 hardware, topology,
row width, pattern, cache and D4 go with every number. C4 capacity, never parity: no number enters a
report, pack, certificate, sign-off, readiness score or dashboard, the UI or the API, and the synthetic gate is
never accuracy evidence. C5 `observed on <hardware>, synthetic data`, never guaranteed, certified or an
SLA. C6 no like-for-like vendor comparison. C7 failures stay in: `did not complete: <reason>`. C8
vendor-measured, not independently verified; reproducible from the file's command and commit, and each
cited file is named with its commit and sha256.

## 9. Commands

```
uv run python tools/bench.py run --out DIR [--rows 100k,1M,10M] [--scenarios sparse,dense,clustered,graph]
    [--shape narrow|wide] [--repeat 3] [--server file:/abs/admin-dsn] [--work DIR] [--timeout 240]
    [--stall 10] [--max-rss BYTES] [--allow-dirty]
uv run python tools/bench.py report FILE... [--draft] [--extrapolate ROWS]
uv run python tools/bench.py clean [--server file:/abs/admin-dsn] [--work DIR]
```

`--rows` items match `^[1-9][0-9]*[kM]?$`. `clean` removes labelled containers with `-v`, or drops marked
databases, and removes marked `dsbench-*` folders under `--work` (default the temp directory); it is for
after a kill and removes a live run's too. **Exits:** 0 all correct; 1 a repetition not correct or an
abort (file kept, `complete: false` if anything was left unrun); 2 refused (§10: no database, no file); 130
interrupted (file keeps what was measured). Each repetition prints `<rows> <scenario> <n>/<repeat>
(<cache>): run <s> s (scan <s> s, <keys/s> keys/s, cycle end <s> s), peak <MB> MB, correct`, or `…:
incorrect (<counter> <observed>, truth <expected>)`, or `…: aborted (<reason>)` with the last 5 lines of the
masked `ds` stderr.

**The Results' host:** stop the other stacks (the harness never does), commit, then `run --out
docs/bench` (about 5 GB at peak) and the
same with `--rows 1M --shape wide --scenarios sparse,dense`; commit both files and `report`'s numbers. They took
41 and 4 minutes here (Results).
**100M:** `run --rows 100M --scenarios sparse,dense,graph --repeat 1 --work /big --out DIR` on Linux with
the image loaded (`docker load` when air-gapped), or `--server`. With no smaller size in the invocation, the
preflight and the re-check both use §3's estimates: about 97.5 GB on the database volume and 18 GB in the work
folder (`sparse,dense` alone: 64.5 GB and 18 GB). The preflight prints nothing when the size fits; B12 names
the figures when it does not. 10M's bytes a row, extrapolated and not measured, give about 11 GB a narrow
table and 6.3 GB of sort temp a side, held until the scan ends, and for `graph` 12 GB a links table and about
20 GB of sort temp written a side in two phases, L1's then L2's, of which about 11 GB a side is held at once,
with about 7.7 GB of evidence (11.5 GB with the preflight's 1.5× margin). 10M `dense` measured about 11.4 KB of peak memory a differing key: about
11.5 GB at 100M, extrapolated (Results).

## 10. Messages (binding; each asserted)

**Refusals** (exit 2) come in three stages, each finished before the next begins. **Stage 1, arguments,
before any subprocess:** **B4** `--work <path> is not a folder`, `--work <path> is in a git working tree:
evidence would sit beside the code`, `--work <path> is in a cloud-synced folder (~/<folder>): use a local
folder`; **B5** `ds is not installed next to <python>: run uv sync`; **B6** `--rows <item>: sizes are
multiples of 10,000 from 10,000 to 1,000,000,000, each once`; **B7** `--scenarios <item>: choose from sparse,
dense, clustered, graph`; **B8** `--server must be file:<absolute path>`; **B13** `--out <path> is not a
folder`; **B14** `the server's DSN names a remote host with sslmode <mode>: use sslmode=verify-full, since
the benchmark connects as a superuser`; also `--shape must be narrow or wide`, `--server: <the secret file's
error>`, `--server: the admin DSN is not a valid connection string` (libpq's message would quote it),
`--server: the admin DSN names a service: write its parameters in the file`, `--server: the admin DSN names
no host: give host= or hostaddr= (a socket folder counts), or libpq takes PGHOST` and `--server:
<PGHOSTADDR|PGSERVICE> is set and the admin DSN names no hostaddr: the harness's own connections would take
it; unset <PGHOSTADDR|PGSERVICE>, or give hostaddr=`, and `--server: <PGPORT|PGUSER|PGPASSWORD|PGSSLMODE> is
set and the admin DSN names no <port|user|password|sslmode>: the harness's own connections would take it;
unset <name>, or give <key>=`. **Stage 2, environment, before any container, database or file:**
**B1** `docker is not available: install Docker, or give --server file:<absolute path>`, `the Docker
endpoint is not a local socket (<scheme>://): the port and the password file would be on another machine;
use a local Docker, or --server file:<absolute path>`; **B2** `postgres:16-alpine (the benchmark's database)
is not in the local image store; this tool never pulls. To add it: docker pull postgres:16-alpine`; **B3**
`deltasleuth/, tools/, pyproject.toml or uv.lock has uncommitted or untracked changes: commit them, or pass
--allow-dirty (the result is then not publishable)`; **B9** `the server holds <n> other databases: give the
benchmark a PostgreSQL server of its own`; **B10** `the server's role is not a superuser: the benchmark
creates, checkpoints and drops databases`; **B11** `a benchmark container (dsbench-<id>) exists: another run
is under way or was killed; if none is running, run: uv run python tools/bench.py clean` (`<n> benchmark
databases exist: another run is under way or was killed; if none is running, run: uv run python
tools/bench.py clean --server file:<absolute path>` under `--server`, after `the server is unreachable
(<Class>): check --server's DSN` when it does not answer). **Stage 3, space, once the container is up and before any database
or file (the container is removed):** **B12** `<rows> rows a side need about <GB> GB free on the database
volume (it has <GB> GB) and <GB> GB in the work folder (it has <GB> GB; on darwin it holds both): free
space, or run fewer sizes` (`; on darwin it holds both` on darwin when the work folder shares the disk
holding Docker Desktop's data, or no such folder exists; on darwin otherwise `…(it has <GB> GB), <GB> GB on
the disk holding Docker Desktop's data (it has <GB> GB) and <GB> GB in the work folder (it has <GB> GB): free
space, or run fewer sizes`; under `--server`, whose database volume
is not checked, `<rows> rows a side need about <GB> GB in the work folder (it has <GB> GB): free space, or run
fewer sizes`). The file is the result file: the work folder, whose password file the container mounts, exists
already and is removed with the container.

**Aborts** (exit 1, file kept): `scan paused: <Class>`, `<worker line> (3 times)`, `stalled: no progress
for <n> min (<last worker line>)`, `peak memory <GB> GB over --max-rss <GB> GB`, `timeout after <n> min`,
`ds exited <code>`, `killed by signal <n>`, `the benchmark database stopped (out of memory|exited
<code>|unreachable)`, `did not run: <rows> rows need about <GB> GB free, <GB> GB left`, `interrupted` (130);
the problems `host slept <s> s` and `restart was not clean (crash recovery)`. The file's `problems` name what
stopped the run: `interrupted`, the stopped database's abort, or `failed (<Class>)` for any other exception
(`exit <code>` for a refusal), which is raised again once the file is written.

**`report`** (exit 2): `<path> is not a deltasleuth-bench/1 result`; `<path> is not publishable (<reasons>):
pass --draft to render it marked DRAFT`; `--extrapolate <rows> is more than 10 times the largest size
measured in <path> (<rows>)`; per scenario under `Extrapolated, not measured`, `more than 10 times its largest
measured size (<rows>): not extrapolated`. Warnings (exit 0): `warning: <path> was measured at <sha12>;
deltasleuth/, tools/, pyproject.toml or uv.lock changed since`; `warning: these files come from <n> hosts: their numbers
are not one curve` (or commits).

**Caveat block** (verbatim): `What these numbers do not show: synthetic data on one host; both sources and
the control plane on one PostgreSQL server, with the settings the conditions line names; no cold-cache figure
("restarted" means the server restarted with its OS page cache warm); worker.poll_seconds 0.05, where the shipped 1 s can add about
a second to each cycle's end and each idle slot's next lease; politeness budgets lifted, where production
reads at most rate_per_s × 200 keys a second per side; the service crawl, rate-bound at T = N·(1+m·t)/r,
not benchmarked; vendor-measured, not independently verified.`

## 11. Product surface: none

No API route, role, audit action (the product audits into each throwaway store), UI, setting, `ds` command,
migration, licence fence or dependency (§16's fix is inside a cycle end and a query); only `docker`, `git`,
`uv`, stock `ps`, `sysctl`, `pmset`, `caffeinate` and the local image.

## 12. Code (`tools/bench.py`)

`SCENARIOS` is a registry (a new scenario is an entry) beside `SHAPES` and §3's estimates; functions
`sizes`, `truth`, `generate`, `Container`/`Server`, `_env`, `_refuse_arguments`, `_refuse_environment`,
`_preflight`, `repetition`, `_watch(proc, poll, clock)` (the poll injected, so the unit tests script it),
`_judge`, `environment`, `summary`, `fit`, `render`, `clean`. Reuse: `dsv.fail`, `file_ref`; `release._run`
(always with `env=ENV`), `ROOT`, `POSTGRES`, `SYNCED` (not `_git`, `_poll` or `_inspect`, which pass no
`env`); `read_secret`; `fsio.atomic_write`, `ensure_private_dir`; `statistics`. It is written like its
siblings: Typer, `pretty_exceptions_enable=False`, a module docstring naming this contract.

## 13. Tests

No image is pulled. **`tests/unit/test_bench_tool.py`** (CliRunner, a stub `docker` as
`tests/unit/releases.py`, a stub `ds`): `test_sizes_parse`; `test_truth_closed_forms` (1M `sparse` 10/1/0;
20k `dense` and `clustered` 160/20/20; graph M = N/10,000); `test_truth_matches_brute_force` (Python
transcriptions of each predicate, N in 10k, 20k, 30k, 120k); `test_summary_and_noisy` (per cache state:
`warm` is the headline, `restarted` apart, a restart effect alone never sets `noisy`);
`test_fit_and_residual`; `test_extrapolate_labelled_and_capped` (the model only under its heading; cycle
end and RSS scaled by D; the model's error per measured size; RSS beside each time; `would not fit in
<memory>: not extrapolated`; the 10× cap); `test_report_refuses_unpublishable`;
`test_report_warns_changed_code_and_mixed_hosts`; `test_caveat_block_verbatim`; `test_rss_bytes_per_platform`;
`test_exit_code_from_wait4_status` (and `proc.returncode` set); `test_env_scrubbed` (children see no `PG*`,
`DS_*` or `DOCKER_CONTEXT`; `os.environ` unchanged after `invoke`); `test_dirty_includes_untracked` (an
untracked `tools/x.py` is dirty, an untracked `docs/x.md` is not); `test_container_argv` (`--pull never`,
loopback, label, `restart -t 300`, no password in any logged argv or environment);
`test_unclean_restart_is_a_problem`; `test_remote_docker_endpoint_refused`; `test_server_dsn_keeps_tls`
(only `dbname` changes; B14 for a remote host below `verify-full`); `test_teardown_when_generation_fails`;
`test_refusals_by_stage` (stage 1 leaves the stub docker's log empty; stage 2 logs no `run`; neither creates
a file or folder; stage 3 refuses a size from the estimates and removes the container; a failed measured
re-check is an abort, exit 1, file kept with `complete: false`); `test_watchdog_aborts` (scripted polls and
the stub `ds`: timeout, max-rss, stall, a scan pause, an `OperationalError` line three times, and
`OOMKilled`, exited and unreachable); `test_result_holds_no_secret_or_host` (`server` a boolean, no path);
`test_clean_removes_marked_work_folders` (unmarked kept); `test_committed_results_publishable_and_cited`
(each cited file publishable with its cited sha256, every number in BENCHMARK.md's Results section in a
cited file; vacuous until the first publish).

**`tests/e2e/test_bench.py`** (`e2e`, `db`; `pg_server` gates Docker, failing under `DS_REQUIRE_DB=1`):
`test_harness_end_to_end_small` (`--rows 20k --repeat 1 --allow-dirty`, all scenarios: exit 0, every
repetition correct, every metric present, `run_s ≥ scan_s + reads_tail_s + cycle_end_s − 0.3 s` and
`run_s ≤ seconds.process`; no labelled container or work folder left; `--out` holds only the JSON);
`test_truth_sql_equals_python` (and the wide expressions at g = 268,435,456 and 10⁹ in SQL);
`test_a_paused_scan_aborts_the_repetition` (side a's database set to `work_mem = '64kB'`,
`temp_file_limit = 0`: `scan paused: ConfigurationLimitExceeded`, exit 1); `test_corrupted_truth_is_incorrect`
(exit 1, not publishable, `report` refuses); `test_server_mode_guard_and_clean` (a planted database refused
with B9 and a non-superuser with B10, no `dsbench_` database made; without them a 10k `sparse` run leaves
none; `clean --server` drops a marked database, keeps an unmarked one). About 40 s. **`DS_PERF=1`:**
`test_the_harness_at_a_million_rows` (`--rows 1M --repeat 1`, all scenarios, every repetition correct; no
time asserted: the four existing DS_PERF tests keep their budgets).

As built: the unit file has no database, so its runs past stage 3 replace `environment`, `_prepare` and
`_size` or `generate`; `test_env_scrubbed` lets the endpoint's one resolving call see the caller's Docker
settings (§3); `test_refusals_by_stage` also asserts §10's additions; `test_watch_times_the_phases` holds
t_progress, t_done (with its count), t_drained and t_end to scripted polls. The Results section cites each file
as `report`'s conditions line does, `` `bench-….json` (commit <sha12>, sha256 <64 hex>) ``, and
`test_committed_results_publishable_and_cited` requires each number of the section's tables in the report of the
files it cites, and of a file that is not publishable, `report --draft`'s `**DRAFT: … is not publishable (…).**`
line in the section (C1: a draft measures no size). The unit file also holds
`test_statistics_states_are_never_pooled` (the findings' own analysis during the cycle end counts as
`analysed`, unless autovacuum analysed them there too), `test_noisy_sizes_are_marked_in_fits_and_the_model` (and the
per-scenario 10× cap in `test_extrapolate_labelled_and_capped`), `test_unrun_sizes_and_scenarios_name_their_cause`,
`test_rows_are_marked_by_file`, `test_first_is_shown_apart_under_server`,
`test_conditions_say_stock_only_for_the_image_s_settings`, `test_a_sleep_is_a_problem`,
`test_server_dsn_is_self_contained` (with `hostaddr` in `test_container_argv` and the new refusals in
`test_refusals_by_stage`), `test_server_guard_messages`, `test_teardown_when_a_second_signal_cuts_the_removal_short`,
`test_sighup_unwinds_as_ctrl_c`, `test_a_stopped_run_keeps_its_file_and_names_what_did_not_run` (Ctrl-C during a
size: exit 130; a stopped database; a generation error), `test_space_counts_docker_desktop_s_disk_on_darwin`
(and `--server`'s B12), `test_measured_takes_the_temp_held_at_once`, a signalled `ds` in `test_watchdog_aborts`
and the temp peak in `test_watch_times_the_phases`; the e2e file asserts each repetition's statistics state
(`analysed`) and, with `Container.restart` stubbed unclean, `restart was not clean (crash recovery)`. Sizes:
about 1,750 tool lines (1,490 not blank), 1,150 unit and 260 e2e test lines; the e2e file takes about 45 s.

## 14. Documents to amend

- **New `docs/mvp/BENCHMARK.md`:** this contract, message formats with placeholders only, and a Results
  section from `report` citing each file by name, commit and sha256.
- **TABLES:** §5 (a refused sort retries without end; temp bytes per row); §7's Performance bullet (the
  6.8 s stays the DS_PERF budget's measurement; BENCHMARK.md's 1M `sparse` is cited as another profile,
  not a re-measurement); §12 (keys/s at 1M and 10M).
- **GRAPH §5:** 0.1M–10M facts, with the group index and `maxComponent: 20`.
- **DEPLOY:** §7 `A database target` (per source: one REPEATABLE READ snapshot for `scan_s`, holding back
  vacuum; one backend sorting the population without a parallel plan, side a before side b; temp bytes per
  row; the endless pause below `temp_file_limit`); §10 `Throughput` (measured keys/s).
- **RUNBOOK:** `Compare two tables` (the same facts, with hardware); `Graph parity` (1M and 10M).
- **CONTRACTS** §1's `tools/` line, §10 a bullet; **README** an MVP bullet and a Layout row.
- **architecture §12:** the intro (`(assumed) values await the P2 benchmark`: table-pair values are
  measured in BENCHMARK.md's published Results) and §12.3's 100M bullet (the extract and hashdiff design is
  not built or measured).
- Every amended passage that gives a benchmark figure gives the Results' warm median with its min–max (C2)
  and C3's conditions (hardware, row width, pattern, cache, D4's poll), keeps a `noisy` flag or statistics
  state the report sets beside it, and says `extrapolated, not measured` of an extrapolation, or points to the Results.

## 15. Seams with other features

It measures the product as built: cycle-end work alerts or readiness add shows in
`cycle_end_s`, the commit telling results apart. **Alerts:** no destination is configured. **Labelled accuracy:** its draw
is not measured, and the synthetic gate is not accuracy (C4); its contract ([ACCURACY.md](ACCURACY.md)
§14) does not cite the benchmark as measuring its draw. Its analysis of the root causes a cycle end links
is part of `cycle_end_s` and, in the statistics state, counts as `analysed` (§7). **Readiness:** its measurement is not timed, and its contract does not
cite the benchmark as timing it; no
benchmark constant enters the score, dashboard or an ETA, which come from the run's own `ds.scans` state
and observations. Either could later be a registry entry with its own truth
and gate.

## 16. Limits, product findings, deferred, size

**Limits.** One Mac: page-cache data and `fsync` without `F_FULLFSYNC` flatter it, the port forward costs
it; a server's curve may differ. Two warm repetitions give a range, not a confidence interval. Stock
settings spill sorts a tuned server would not. A store rename breaks the harness (the e2e test catches it).
The retry guards match log text; the stall watchdog and `--timeout` back them, and the watchdog stops at
t_drained, so the cycle end is bounded only by `--timeout` and `--max-rss`. User-namespace remapping or
enforcing SELinux may refuse the password mount: use `--server`.

**Product findings** (not fixed here): a scan refused for `temp_file_limit` or a full disk
retries forever while its run shows `running`, and so do per-key saves, worker slots and sweeps on an
`OperationalError` (`worker._handle`, `slot`, `sweep`); `ds.runs.finished_at` is `now()` in the ending
transaction, whose first statement is the row lock, so it records the start of the cycle end, not its
finish; cycle-end memory grows with differing keys (`cycle.summarise` keeps every differing key's result
and findings, `compare.rederive_all` their observations at once: about 11.4 KB a differing key at
10M), so 100M `dense` may exhaust a small host's memory; the scan's `ORDER BY convert_to(<key text>, 'UTF8')`
uses no index and a `DECLARE CURSOR` never gets a parallel plan, so each source sorts on one core.

**Product finding fixed here:** on a store whose tables had no statistics yet (a new store's first large run,
or each repetition here until autovacuum analysed it), `repo.list_findings`, which `repo.pages` calls 1,000
rows at a time, joined findings to `run_keys` and `results` by nested loops on `(run_id, cycle)` alone, so each
page read every finding once per key: quadratic in differing keys (at 1M, 60 million rows through the join
filter and 10.7 s for one page, in a diagnostic run *not cited*). A cycle end now first analyses `run_keys`,
`results`, `findings` and `observations` when one of them holds rows but no column statistics
(`repo.analyse_new`, from `cycle.finish_cycle` before the row lock), and a page of `repo.list_findings` seeks its
first key (`k.seq >= key_after`) instead of joining every earlier key again. `tests/unit/store/test_repo.py`
holds both by the rows each page's `EXPLAIN ANALYZE` reads, no timing; `tests/unit/runner/test_scan.py` that a
first cycle end analyses the tables. Tables analysed before a later run's rows arrived keep those statistics
until autovacuum analyses them again; each repetition records the statistics its store had (§7). The Results
files were measured after the fix, on `10f295c`: in 25 of their 42 repetitions `repo.analyse_new` analysed the
store just before the cycle end, and in 11 autovacuum had analysed it earlier; in the six at 10M `dense` and
`clustered` neither had, yet `repo.analyse_new` did not analyse, and autovacuum analysed the tables during the
cycle end, which the report marks. The cause: `10f295c` analysed only while a table had `reltuples < 0`, and
autovacuum's insert-driven VACUUM had already counted the rows without gathering statistics. Since then a
table with rows but no statistics triggers it (`test_a_new_store_s_first_cycle_end_analyses_its_tables`
VACUUMs the tables first); the Results predate that change and are not re-measured.

**Deferred:** a cold-cache mode, remote-source topologies, labelled-accuracy and readiness entries. **Size:** §13 has
the built sizes; about 20 product lines for the fix above, and no migration or dependency.

## Results

**Published: C1 counts 0.1M, 1M and 10M narrow rows a side and 1M wide rows a side as measured; 100M stays
`not yet measured`.** Both files were measured on 2026-09-30 from the committed harness at `10f295c`, after
§16's fix, and each is complete, clean (`commit.dirty` false) and correct in every repetition, so `report`
renders them without `--draft`; each records every repetition's statistics state (§7). The other stacks on this
Mac (seven containers) kept running, since the harness never stops them: their CPU at each quiet check is in
the files, and `busy` marks the rows where it reached 10% (at most 31.6%, the 1M `sparse` second repetition's).
The load average stayed at or under 5.3 of 16 CPUs; no repetition ran on battery, slept or followed an unclean
restart. Two other flags stand beside the numbers, never blocking: the 1M `dense` and `clustered` rows are
`noisy`, with those containers running (below), and the 10M `dense` and `clustered` rows' statistics state is
`analysed during the cycle end` (below).

The runs: `uv run python tools/bench.py run --out DIR` (0.1M, 1M and 10M rows a side, all four scenarios,
`--repeat 3`: 41 minutes, 36 of 36 repetitions correct) and the same with `--rows 1M --shape wide --scenarios
sparse,dense` (4 minutes, 6 of 6 correct), both files then copied unchanged into `docs/bench/`.
`report` of both files, verbatim:

`bench-20260930T143741Z-10f295cf6e88.json` (commit 10f295cf6e88, sha256 29aaf6e234424faf8923f25de98dd6e97feeef5c8d4941f70cfe34ec5e8b3098): observed on Mac15,9 (Apple M3 Max, 16 CPUs, 68.7 GB, darwin), synthetic data; Docker 29.1.3 (Docker Desktop, 16 CPUs, 17.0 GB, cgroup v2); PostgreSQL 16.14 (postgres:16-alpine) with stock settings holding both sources and the control plane; topology container, rtt 0.232 ms; worker concurrency 4, lease 120 s, poll 0.05 s (D4); narrow rows.
`bench-20260930T151853Z-10f295cf6e88.json` (commit 10f295cf6e88, sha256 75ce3017fb76f7037be07ee89436b228fa0471b4e4f36cb1327f1fea870af717): observed on Mac15,9 (Apple M3 Max, 16 CPUs, 68.7 GB, darwin), synthetic data; Docker 29.1.3 (Docker Desktop, 16 CPUs, 17.0 GB, cgroup v2); PostgreSQL 16.14 (postgres:16-alpine) with stock settings holding both sources and the control plane; topology container, rtt 0.24 ms; worker concurrency 4, lease 120 s, poll 0.05 s (D4); wide rows.

**narrow rows** (users: 4 columns, 71 B a row; links: 3 columns, 41 B a row): headline medians (min–max, n)

| File | Scenario | Rows a side | D | State | run s | first progress s | scan s | L1 s | L2 s | reads tail s | cycle end s | keys/s | tail keys/s | rows/s | peak MB | ds CPU s | database CPU s | temp MB | stored B per differing key | restarted/first run s | restarted/first scan s | Flags |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `bench-20260930T143741Z-10f295cf6e88.json` | `sparse` | 100k | 2 | warm | 0.77 (0.76–0.77, n 2) | 0.40 (0.40–0.40, n 2) | 0.72 (0.72–0.73, n 2) | - | - | 0.00 (0.00–0.00, n 2) | 0.00 (0.00–0.00, n 2) | - | - | 138,322 (137,174–139,470, n 2) | 102 (102–102, n 2) | 0.71 (0.70–0.72, n 2) | 0.64 (0.63–0.65, n 2) | 12 (12–12, n 2) | 224,770 (208,386–241,154, n 2) | 0.89 | 0.73 |  |
| `bench-20260930T143741Z-10f295cf6e88.json` | `dense` | 100k | 1,000 | warm | 2.13 (2.12–2.14, n 2) | 0.46 (0.42–0.50, n 2) | 0.83 (0.82–0.84, n 2) | - | - | 0.95 (0.95–0.95, n 2) | 0.31 (0.31–0.31, n 2) | - | - | 120,865 (119,332–122,399, n 2) | 108 (107–108, n 2) | 2.18 (2.18–2.18, n 2) | 1.19 (1.15–1.22, n 2) | 12 (12–12, n 2) | 4,783 (4,632–4,934, n 2) | 2.10 | 0.81 |  |
| `bench-20260930T143741Z-10f295cf6e88.json` | `clustered` | 100k | 1,000 | warm | 2.14 (2.14–2.14, n 2) | 0.49 (0.48–0.51, n 2) | 0.87 (0.82–0.92, n 2) | - | - | 0.91 (0.85–0.95, n 2) | 0.32 (0.32–0.32, n 2) | - | - | 115,116 (108,578–121,654, n 2) | 108 (108–108, n 2) | 2.18 (2.17–2.19, n 2) | 1.19 (1.18–1.21, n 2) | 12 (12–12, n 2) | 4,802 (4,704–4,899, n 2) | 2.13 | 0.82 |  |
| `bench-20260930T143741Z-10f295cf6e88.json` | `graph` | 100k | 20 | warm | 1.63 (1.61–1.65, n 2) | 0.41 (0.40–0.42, n 2) | 1.48 (1.46–1.50, n 2) | 0.73 (0.72–0.73, n 2) | 0.75 (0.74–0.76, n 2) | 0.00 (0.00–0.00, n 2) | 0.11 (0.11–0.11, n 2) | - | - | 67,741 (66,800–68,681, n 2) | 110 (110–111, n 2) | 1.06 (1.06–1.07, n 2) | 1.20 (1.20–1.20, n 2) | 37 (37–37, n 2) | 29,310 (24,228–34,392, n 2) | 1.63 | 1.47 |  |
| `bench-20260930T143741Z-10f295cf6e88.json` | `sparse` | 1M | 11 | warm | 6.96 (6.95–6.96, n 2) | 3.67 (3.61–3.72, n 2) | 6.84 (6.79–6.89, n 2) | - | - | 0.00 (0.00–0.00, n 2) | 0.05 (0.00–0.11, n 2) | - | - | 146,249 (145,180–147,319, n 2) | 102 (102–102, n 2) | 3.30 (3.30–3.30, n 2) | 4.79 (4.78–4.81, n 2) | 122 (122–122, n 2) | 47,108 (44,873–49,342, n 2) | 6.94 | 6.88 | busy |
| `bench-20260930T143741Z-10f295cf6e88.json` | `dense` | 1M | 10,000 | warm | 23.1 (21.1–25.1, n 2) | 3.77 (3.73–3.81, n 2) | 7.82 (7.77–7.87, n 2) | - | - | 12.5 (10.7–14.4, n 2) | 2.68 (2.55–2.81, n 2) | 627 (558–696, n 2) | 731 (616–846, n 2) | 127,915 (127,146–128,684, n 2) | 213 (212–213, n 2) | 17.7 (17.7–17.8, n 2) | 39.3 (29.8–48.7, n 2) | 122 (122–122, n 2) | 4,266 (4,175–4,356, n 2) | 19.3 | 7.72 | noisy |
| `bench-20260930T143741Z-10f295cf6e88.json` | `clustered` | 1M | 10,000 | warm | 23.0 (19.7–26.3, n 2) | 3.66 (3.63–3.69, n 2) | 7.58 (7.58–7.58, n 2) | - | - | 12.6 (9.28–15.9, n 2) | 2.75 (2.74–2.76, n 2) | 670 (546–794, n 2) | 767 (565–970, n 2) | 131,926 (131,839–132,013, n 2) | 214 (214–215, n 2) | 18.0 (17.8–18.2, n 2) | 33.2 (12.2–54.3, n 2) | 122 (122–122, n 2) | 4,335 (4,311–4,359, n 2) | 19.5 | 7.66 | noisy |
| `bench-20260930T143741Z-10f295cf6e88.json` | `graph` | 1M | 200 | warm | 17.9 (17.9–18.0, n 2) | 3.27 (3.26–3.29, n 2) | 17.8 (17.7–17.8, n 2) | 7.19 (7.16–7.21, n 2) | 10.6 (10.5–10.6, n 2) | 0.00 (0.00–0.00, n 2) | 0.11 (0.11–0.12, n 2) | - | - | 56,282 (56,098–56,465, n 2) | 112 (112–112, n 2) | 6.68 (6.65–6.70, n 2) | 14.1 (14.1–14.1, n 2) | 422 (422–422, n 2) | 6,924 (6,739–7,108, n 2) | 17.9 | 17.9 |  |
| `bench-20260930T143741Z-10f295cf6e88.json` | `sparse` | 10M | 101 | warm | 69.1 (68.9–69.3, n 2) | 36.6 (36.6–36.7, n 2) | 68.9 (68.7–69.1, n 2) | - | - | 0.00 (0.00–0.00, n 2) | 0.12 (0.12–0.12, n 2) | - | - | 145,144 (144,659–145,628, n 2) | 103 (103–103, n 2) | 29.5 (29.5–29.5, n 2) | 45.9 (45.9–45.9, n 2) | 1,258 (1,258–1,258, n 2) | 8,944 (8,782–9,106, n 2) | 69.1 | 68.9 | busy |
| `bench-20260930T143741Z-10f295cf6e88.json` | `dense` | 10M | 100,000 | warm | 211.9 (211.4–212.3, n 2) | 36.9 (36.9–36.9, n 2) | 77.5 (77.4–77.6, n 2) | - | - | 93.1 (92.7–93.6, n 2) | 41.2 (41.1–41.3, n 2) | 749 (747–752, n 2) | 919 (918–920, n 2) | 129,032 (128,878–129,187, n 2) | 1,239 (1,239–1,239, n 2) | 185.3 (185.0–185.5, n 2) | 137.0 (130.6–143.5, n 2) | 1,258 (1,258–1,258, n 2) | 3,946 (3,937–3,956, n 2) | 211.9 | 77.1 | analysed during the cycle end |
| `bench-20260930T143741Z-10f295cf6e88.json` | `clustered` | 10M | 100,000 | warm | 212.0 (211.3–212.8, n 2) | 37.4 (37.4–37.5, n 2) | 76.6 (76.5–76.7, n 2) | - | - | 94.2 (94.1–94.2, n 2) | 41.2 (40.4–42.0, n 2) | 752 (752–753, n 2) | 930 (928–932, n 2) | 130,488 (130,330–130,646, n 2) | 1,245 (1,244–1,246, n 2) | 184.6 (184.6–184.6, n 2) | 129.6 (127.5–131.7, n 2) | 1,258 (1,258–1,258, n 2) | 3,961 (3,950–3,972, n 2) | 213.6 | 76.7 | busy, analysed during the cycle end |
| `bench-20260930T143741Z-10f295cf6e88.json` | `graph` | 10M | 2,000 | warm | 143.9 (143.7–144.1, n 2) | 32.2 (32.2–32.2, n 2) | 143.5 (143.3–143.6, n 2) | 72.4 (72.3–72.4, n 2) | 71.1 (71.0–71.2, n 2) | 0.00 (0.00–0.00, n 2) | 0.38 (0.32–0.43, n 2) | 45 (45–45, n 2) | - | 69,695 (69,632–69,759, n 2) | 119 (119–119, n 2) | 60.7 (60.5–60.8, n 2) | 102.0 (102.0–102.0, n 2) | 4,029 (4,029–4,029, n 2) | 3,731 (3,725–3,737, n 2) | 144.6 | 144.1 |  |

**wide rows** (users: 24 columns, 389 B a row): headline medians (min–max, n)

| File | Scenario | Rows a side | D | State | run s | first progress s | scan s | L1 s | L2 s | reads tail s | cycle end s | keys/s | tail keys/s | rows/s | peak MB | ds CPU s | database CPU s | temp MB | stored B per differing key | restarted/first run s | restarted/first scan s | Flags |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `bench-20260930T151853Z-10f295cf6e88.json` | `sparse` | 1M | 11 | warm | 16.2 (16.1–16.2, n 2) | 12.9 (12.9–12.9, n 2) | 16.1 (16.1–16.1, n 2) | - | - | 0.00 (0.00–0.00, n 2) | 0.06 (0.00–0.12, n 2) | - | - | 62,270 (62,247–62,294, n 2) | 102 (102–102, n 2) | 4.04 (4.02–4.05, n 2) | 14.2 (14.2–14.2, n 2) | 122 (122–122, n 2) | 46,154 (45,409–46,899, n 2) | 16.2 | 16.2 |  |
| `bench-20260930T151853Z-10f295cf6e88.json` | `dense` | 1M | 10,000 | warm | 41.9 (41.3–42.5, n 2) | 13.0 (12.9–13.0, n 2) | 17.4 (17.4–17.5, n 2) | - | - | 19.2 (18.7–19.8, n 2) | 5.14 (5.12–5.15, n 2) | 454 (443–466, n 2) | 471 (458–484, n 2) | 57,346 (57,205–57,488, n 2) | 289 (289–289, n 2) | 25.3 (25.3–25.4, n 2) | 62.8 (62.2–63.4, n 2) | 122 (122–122, n 2) | 4,863 (4,850–4,876, n 2) | 39.9 | 17.2 | busy |

**Fits** by least squares over one file's sizes, N rows a side and D differing keys:

- `bench-20260930T143741Z-10f295cf6e88.json` `sparse`: run_s ≈ 0.06633 + 6.901e-06·N (largest relative residual 1%); scan_s ≈ -0.007 + 6.89e-06·N (largest relative residual 6%); first_progress_s ≈ 0.0225 + 3.662e-06·N (largest relative residual 4%); cycle_end_s ≈ 0.01963 + 0.001005·D (largest relative residual 44%); peak_rss_bytes ≈ 1.019e+08 + 7269·D (largest relative residual 0%); run_s's exponent between 1M and 10M: 1.00
- `bench-20260930T143741Z-10f295cf6e88.json` `dense`: run_s ≈ 0.9646 + 2.11e-05·N (largest relative residual 45%; noisy at 1M); scan_s ≈ 0.06317 + 7.744e-06·N (largest relative residual 1%; noisy at 1M); first_progress_s ≈ 0.09136 + 3.683e-06·N (largest relative residual 0%; noisy at 1M); cycle_end_s ≈ -0.7769 + 0.000419·D (largest relative residual 215%; noisy at 1M); peak_rss_bytes ≈ 9.737e+07 + 1.142e+04·D (largest relative residual 1%; noisy at 1M); run_s's exponent between 1M and 10M: none, noisy at 1M
- `bench-20260930T143741Z-10f295cf6e88.json` `clustered`: run_s ≈ 0.9063 + 2.112e-05·N (largest relative residual 41%; noisy at 1M); scan_s ≈ 0.01664 + 7.661e-06·N (largest relative residual 10%; noisy at 1M); first_progress_s ≈ 0.02586 + 3.74e-06·N (largest relative residual 19%; noisy at 1M); cycle_end_s ≈ -0.7347 + 0.0004186·D (largest relative residual 198%; noisy at 1M); peak_rss_bytes ≈ 9.807e+07 + 1.147e+04·D (largest relative residual 1%; noisy at 1M); run_s's exponent between 1M and 10M: none, noisy at 1M
- `bench-20260930T143741Z-10f295cf6e88.json` `graph`: run_s ≈ 1.883 + 1.422e-05·N (largest relative residual 103%); scan_s ≈ 1.733 + 1.419e-05·N (largest relative residual 113%); first_progress_s ≈ 0.07664 + 3.213e-06·N (largest relative residual 3%); cycle_end_s ≈ 0.09775 + 0.0001386·D (largest relative residual 10%); peak_rss_bytes ≈ 1.106e+08 + 4300·D (largest relative residual 0%); run_s's exponent between 1M and 10M: 0.90
- `bench-20260930T151853Z-10f295cf6e88.json` `sparse`: 1 size measured: no fit
- `bench-20260930T151853Z-10f295cf6e88.json` `dense`: 1 size measured: no fit

Not measured: the service crawl and mixed pairs, rate-bound at T = N·(1+m·t)/r (architecture §12.1); remote sources beyond rtt_ms; a cold cache; soak re-diffs; concurrent runs; other engines; scale-out; the release image; the accuracy draw and the readiness measurement.

What these numbers do not show: synthetic data on one host; both sources and the control plane on one PostgreSQL server, with the settings the conditions line names; no cold-cache figure ("restarted" means the server restarted with its OS page cache warm); worker.poll_seconds 0.05, where the shipped 1 s can add about a second to each cycle's end and each idle slot's next lease; politeness budgets lifted, where production reads at most rate_per_s × 200 keys a second per side; the service crawl, rate-bound at T = N·(1+m·t)/r, not benchmarked; vendor-measured, not independently verified.

`report --extrapolate 100M` of the narrow file adds this block, verbatim: an extrapolation, not a measurement
(D7):

**Extrapolated, not measured**

T ≈ max(scan_s, first_progress_s + D / tail_keys_per_s) + cycle_end_s: each term from its fit, D the differing keys, the tail rate from the largest size.
- `bench-20260930T143741Z-10f295cf6e88.json` `sparse`: the model's error at 100k -8%, 1M -1%, 10M +0%; at 100M: T ≈ 690 s beside peak RSS ≈ 0.1 GB (tail rate not measured: under 2,000 keys)
- `bench-20260930T143741Z-10f295cf6e88.json` `dense`: the model's error at 100k -44%, 1M -22% (noisy), 10M -12%; at 100M: T ≈ 1,875 s beside peak RSS ≈ 11.5 GB; its fits use noisy sizes (1M)
- `bench-20260930T143741Z-10f295cf6e88.json` `clustered`: the model's error at 100k -46%, 1M -22% (noisy), 10M -12%; at 100M: T ≈ 1,867 s beside peak RSS ≈ 11.6 GB; its fits use noisy sizes (1M)
- `bench-20260930T143741Z-10f295cf6e88.json` `graph`: the model's error at 100k +99%, 1M -11%, 10M +0%; at 100M: T ≈ 1,424 s beside peak RSS ≈ 0.2 GB (tail rate not measured: under 2,000 keys)

**What the files show** (figures from the two files unless marked *not cited*; each a warm median with its
min–max unless it says otherwise):

- **Correct at every size.** All 42 repetitions met §4's truth; every size fitted in Docker's memory, so the
  sources were memory reads.
- **The scan is linear in N.** `sparse` ran 0.77 (0.76–0.77) s, 6.96 (6.95–6.96) s and 69.1 (68.9–69.3) s
  (`run_s`'s exponent 1.00 between 1M and 10M), 145,144 (144,659–145,628) rows a side a second at 10M. Both
  sorts, side a's then side b's, took 36.6 (36.6–36.7) s of 10M's 68.9 (68.7–69.1) s scan. A wide row costs 2.4
  times as much to scan (16.1 (16.1–16.1) s at 1M, against 6.84 (6.79–6.89) s); both shapes' sorts wrote 61 to
  63 bytes a row a side, and the server held both sides' files at once until the scan ended.
- **Per-key reads**, narrow rows with 1% of keys differing: 627 (558–696) keys/s at 1M `dense`, 670 (546–794)
  at 1M `clustered`, 749 (747–752) and 752 (752–753) at 10M `dense` and `clustered`; once the scan had ended,
  731 (616–846), 767 (565–970), 919 (918–920) and 930 (928–932). Wide rows, 1M `dense`: 454 (443–466), and 471
  (458–484) once the scan had ended. Reads, not the budget (200,000 keys/s here), bound them.
- **The noisy 1M rows.** 1M `dense` and `clustered` each had one slower warm repetition: `run_s` 25.1 s against
  21.1 s, and 26.3 s against 19.7 s (restarted: 19.3 s and 19.5 s). The difference is in the reads after the
  scan (14.4 s against 10.7 s, 15.9 s against 9.28 s), while the benchmark's own database used more CPU (48.7 s
  against 29.8 s, 54.3 s against 12.2 s); the six repetitions' scans (7.58 to 7.87 s) and cycle ends (2.55 to
  2.81 s) barely moved. The seven other containers kept running (the harness never stops them); at these repetitions' quiet
  checks, the only time the files sample them, they used 2.8% to 8.1% CPU. The files do not isolate the cause.
  They also show that the two slower repetitions are the two whose store no one analysed during the reads (the
  product's `repo.analyse_new` analysed it about 0.1 s before the cycle end), where autovacuum analysed
  `run_keys` 3.3 to 12.2 s before the cycle end in the other four; §7 counts all six `analysed`, since it judges
  the cycle end only.
- **The cycle end**, narrow: `dense` 0.31 (0.31–0.31) s at 1,000 differing keys, 2.68 (2.55–2.81) s at 10,000
  and 41.2 (41.1–41.3) s at 100,000; `clustered` 0.32 (0.32–0.32), 2.75 (2.74–2.76) and 41.2 (40.4–42.0) s. The
  store was analysed before the cycle end began at 0.1M and 1M, and during it at 10M: in all six 10M `dense` and
  `clustered` repetitions, restarted included, autovacuum analysed the four store tables 13 to 24 s into a cycle
  end of 40.4 to 42.5 s, and the product's own analysis (`repo.analyse_new`, §16), which at `10f295c` ran
  only while one of them had `reltuples < 0`, did not run (no table has an `analyze_s`; it now runs on rows
  without statistics, §16). So from 10,000 to 100,000 differing keys
  the cycle end is shown in two statistics states, not as one curve. The Fits mark both scenarios' cycle-end
  terms noisy at 1M (from those rows' `run_s`; their cycle ends spread under 10%); their 215% and 198%
  residuals are the lines' misses at 0.1M, where the negative intercepts predict less than nothing, and say
  nothing about 100M.
- **Memory grows with differing keys, not rows:** about 11.4 KB of peak RSS a differing key (`dense`'s fit,
  1.142e+04·D; `clustered`'s 1.147e+04; both marked noisy at 1M, where the peaks were 213 (212–213) and 214
  (214–215) MB), 1,239 (1,239–1,239) MB at 10M `dense`; `sparse` peaks near 103 MB at every size.
  Extrapolated, not measured: about 11.5 GB for 100M `dense`'s million differing keys, within this Mac's
  68.7 GB.
- **Stored:** about 3,650 bytes of store and 7,690 of evidence a differing key at 10M, as §3's re-check counts
  them from one repetition (320 bytes of content in 1.8 files, a 4 KiB block each).
- **Graph:** 10M facts a side in 143.9 (143.7–144.1) s, L1 72.4 (72.3–72.4) s and L2 71.1 (71.0–71.2) s
  (`maxComponent: 20`, with GRAPH §5's group index). Its sorts wrote 211 and 201 bytes a fact a side at 1M and
  10M, in two phases, and the server held at most 121 and 107 of them at once. Its keys/s is the rate the scan
  found differing identifiers, not a read rate.
- **Wide rows, after §16's fix:** 1M wide `dense` ran 41.9 (41.3–42.5) s with a 5.14 (5.12–5.15) s cycle end,
  against narrow's 23.1 (21.1–25.1) s and 2.68 (2.55–2.81) s; its scan took 17.4 (17.4–17.5) s against 7.82
  (7.77–7.87) s. The row is `busy`: the other containers used 25.6% CPU at its restarted repetition's quiet
  check.
- **100M: not yet run.** For §9's command (`sparse`, `dense`, `graph`, one size) the preflight uses §3's
  estimates, since nothing smaller is measured in that invocation: about 97.5 GB on the database volume and
  18 GB in the work folder; `sparse` and `dense` alone need about 64.5 GB and 18 GB. On this Mac the database
  volume check alone would refuse it: its Docker VM had 69.8 GB free (*not cited*: the VM's `df`, which the
  files do not record). It needs a Linux host with the disk (`--work /big`) or a dedicated
  server (`--server`).

## A service pair at a million keys (measured by hand, 2026-10-07 and 08)

Not a harness result and not a C1 claim: one observation, made on the same Mac while other work ran on it.
Measured by hand; the scripts are not shipped.
A batch run of 1,000,000 keys between two generated stand-in REST services on loopback, which the
repository does not ship (two requests a key, about 4 kB an
answer; one known-difference finding a key; 1.27% of keys planted as different), started with a `--keys` file,
one worker process, targets at `rate_per_s: 2000` and `max_concurrency: 32`, spec `concurrency: 32`:

- **Completed in 2 h 27 min**, 130 to 140 keys a second: 987,334 KNOWN_DIFF and 12,666 MISMATCH, exactly the
  planted ones. Creating the run took 17 s. One worker process is active at a time (others stand by), so on
  this machine that rate is an installation's ceiling; a service's own rate budget is usually far below it.
- **Found and fixed by this run.** `repo.renew_lease` had no index and scanned `ds.run_keys` at every
  renewal: about 5 keys a second at a million keys. Migration `017` (`run_keys_leased_idx`) fixes it. The
  cycle's end and the reports read every result and finding into memory: 3.2 GB held after the run. They are
  paged now: at 100,000 keys the worker's peak fell from 856 MB to 290 MB, `report.pdf` from 2.4 s to 0.14 s
  and `root_causes.csv` from 3.0 s to 0.02 s, with every stored counter and every export byte for byte the
  same. The paged code was measured at 100,000 keys only, not again at a million. Its reads walk a
  cycle's keys in order and read each key's rows by key, also where the planner's statistics do not hold
  the cycle (a run small beside the store, a soak's later cycle: there the first paged statements read
  the whole cycle for every page); `tests/unit/store/test_repo.py` holds that by the rows each plan
  reads, and it was not timed. Where a sample's member was read again from its first pass, the re-read
  its rows cite (`recheck_obs_a_id`, `recheck_obs_b_id`) is the latest of its highest pass.
- **Store**: about 3.6 kB a key (observations 1.4, findings 1.3, results 0.5, run keys 0.4): 3.6 GB at a
  million keys; the evidence directory adds about 7.8 kB a key.
- **Still growing with a run's size**: a cycle's end holds the discrepant keys' results and findings (about
  58 kB resident a discrepant key), and `discrepancies.csv`, `normalization.csv` and `retry.csv` walk every
  result (about 2 s per 100,000 keys). Give a larger population as several runs of up to a million keys.
