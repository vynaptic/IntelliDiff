# DeltaSleuth

DeltaSleuth checks whether two services agree. It calls both endpoints (or reads captured paired
responses), turns each response into a canonical document, finds every per-key difference
deterministically, groups the differences into probable root causes, and explains them, with every
reported value masked.

It is black-box: a target is an endpoint plus its declared, observable behaviour (which not-found
responses are authoritative, how truncation and pagination are signalled). How either service is
built never matters.

This repository holds the **MVP**:

- batch, soak and one-shot runs on a database that survive a killed worker, with rechecks and stop conditions;
- PostgreSQL tables or views as sides: a scan of two tables, then batched per-key reads of what differs ([TABLES.md](docs/mvp/TABLES.md));
- MySQL and Oracle tables or views as sides too, in any pairing with each other, PostgreSQL and a service: one key order and one canonical rendering across engines, rows hashed by the server within one engine and by the worker across engines, and `ds target check` as the pre-flight ([ENGINES.md](docs/mvp/ENGINES.md); the acceptance kits are `examples/tables/mysql/` and `examples/tables/oracle/`). Prerequisites: observed on MySQL 8.4.11 and Oracle 23.26.3 only (8.0 and 19c are supported, not observed: run the acceptance kit); Oracle over plain TCP or TCPS, not Native Network Encryption; in an Oracle database that is not AL32UTF8, a run takes text keys of up to 500 bytes, by a conversion observed on `NVARCHAR2` only; MariaDB and SQL Server are not supported; `ds target check` reports each of these before a run is attempted. Measured once each, on one Mac that was not idle, every database a stock container on it, 4-column synthetic rows, 11 differing keys ([TABLES.md](docs/mvp/TABLES.md) §13.9): 1M rows a side in 27.9 s between PostgreSQL and MySQL, 25.9 s between PostgreSQL and Oracle, 23.0 s between MySQL and Oracle, 6.4 s between two MySQL tables and 6.2 s between two Oracle tables (two PostgreSQL tables: 6.8 s); 10M rows a side scanned in 280 s between PostgreSQL and MySQL, 3.6 times the 77.5 s of two PostgreSQL tables in BENCHMARK, and in 64 s between two MySQL tables, the worker's process peaking at 191 MiB and 180 MiB, with `GET /v1/runs/{id}` answering in 162 ms and 65 ms at the 95th percentile meanwhile, 10 ms with `ds serve --no-worker` beside `ds worker`; 20,000 differing keys of 100,000 in 38.2 s, 35.9 s and 34.1 s across engines. Not measured: 10M rows with an Oracle side, MySQL 8.0, Oracle 19c, wider rows, a database on another host;
- CSV and Parquet files as sides too, in a directory the host mounts read-only, in any pairing with a database, a service or another export: read in place by one DuckDB locked to the declared directories, which runs in a child process of its own so that a fault or a memory blow-up of DuckDB's ends that child and never the worker or the API ([READER.md](docs/mvp/READER.md): not a sandbox; on Linux it sets itself a data limit and denies itself sockets), pinned by SHA-256 when the run is created (a change that a later check sees fails the run; what a writer of the export directory can do unseen is in [FILES.md](docs/mvp/FILES.md) §14), a CSV's dialect declared in the approved spec and its lines that are not rows counted, never dropped; `ds target check` drafts a dialect and prints the manifest a pack's digest is made of. Observed with DuckDB 1.5.5 and files of its own writer; no object storage. Measured on one Mac on 2026-10-04 with the reader in its own process, each figure a single run of its `DS_PERF` test in a pytest process of its own on a machine that was not idle: 1M rows a side compared in 6 s (Parquet against Parquet) to 32 s (CSV against PostgreSQL), 10M rows a side in 53 s (Parquet against Parquet), 183 s (CSV against CSV) and 283 s of scan (Parquet against PostgreSQL, the worker hashing inside `ds serve` or beside it); the in-process reader of 2026-10-03 had taken 5 s to 31 s, 54 s, 181 s and 277 s (274 s beside), and the same sitting's run of it 6 s, 56 s and 185 s for the file-to-file cases; Parquet of other writers and network mounts were not measured, and no figure is Linux's (there the reader's start, its limits and its ends were tested in an image built from source on aarch64 natively; on x86_64 only under emulation and without its limits, never on an x86_64 kernel; and in that image a CSV side whose key is declared by `csv.keys`, as these tests' CSV sides are, took about six times the Mac's pass, 64 s for 10 s over 10M rows, by DuckDB's Linux wheel and not by the reader: measure CSV exports on the host they will run on) ([FILES.md](docs/mvp/FILES.md); [READER.md](docs/mvp/READER.md); [TABLES.md](docs/mvp/TABLES.md) §14.9; the acceptance kit is in `examples/tables/files/`);
- row filters on a table side (`table.where`), a row selected on one side only reported as `FILTER_MEMBERSHIP`;
- key maps (`prefix_pad`, `regex`) and type maps (`typeMap`) for tables that spell keys or store values differently;
- scheduled re-diffs of two tables: a soak that scans both every cycle and lists outcome changes;
- bulk graph parity over two link-fact tables: missing links (L1), then groups and components that split or merge (L2, [GRAPH.md](docs/mvp/GRAPH.md));
- CSV and PDF reports, and an AI agent that answers with verified citations (optional);
- an MCP server for a team's own agent (Claude Code, VS Code, Cursor and the like): eleven tools, ten reads and one one-key compare, over the chat's tools and the API's reads, with the chat's masking, caps and ids, for a named API token whose roles are cut to viewer and operator inside it (on the other `/v1` routes the token keeps its own role); stateless HTTP (`POST /v1/mcp` in `ds serve`) and stdio (`ds mcp`, a developer's workstation); off unless the settings state what tool results may carry. DeltaSleuth checks no answer an external agent writes, a result leaves its control, and masked is not anonymous; observed with the MCP library's own client at the pinned version, client configurations being syntax-checked only ([MCP.md](docs/mvp/MCP.md));
- a public API with a small UI, the `ds` command line, and the P0 onboarding tools (profile, measure, adjudicate, score, report);
- OIDC sign-in, named API tokens, six roles and a per-user audit log ([AUTH.md](docs/mvp/AUTH.md));
- four-eyes approvals and sign-off ([APPROVALS.md](docs/mvp/APPROVALS.md)), and signed evidence packs over a hash-chained audit log ([PACK.md](docs/mvp/PACK.md));
- sign-off sampling for populations too large for a census: a pre-registered, stratified hash sample nobody chooses, drawn from a table snapshot or a complete key listing, judged per stratum on an exact one-sided 95% bound, with a ledger of every draw and sampled evidence packs ([SAMPLING.md](docs/mvp/SAMPLING.md));
- labelled accuracy ("which side is right"): a uniform salted sample of an ended cycle's discrepancies, labelled offline by a named person against a named truth source and counted with exact two-sided 95% intervals; DeltaSleuth still decides no side ([ACCURACY.md](docs/mvp/ACCURACY.md));
- no licence to install: this build is an open build, which embeds no vendor key and enforces no licence, so pairs are not limited and runs under SSO, four-eyes sign-off and evidence packs ask for none; the licence-key mechanism (a vendor-signed file per installation, checked offline, that sets the pairs, the period and the tier) stays in the code, switched off, for a vendor build that embeds keys ([LICENCE.md](docs/mvp/LICENCE.md) §19);
- alerts and notifications during a parallel run: signed HTTPS webhooks, SMTP e-mail, Slack and Teams, routed by rules in the settings, fed from the audit hash chain, with retries, mutes and ids, counts and codes only ([ALERTS.md](docs/mvp/ALERTS.md));
- cut-over readiness and a multi-pair dashboard: per pair, programme and entity type, which named gates hold on stored evidence, how far each is from its target, whether parity converges across soak cycles and what is missing before cut-over, as `k of m gates` and a place on a fixed ladder, never a probability, a forecast or sign-off evidence ([READINESS.md](docs/mvp/READINESS.md));
- table pre-checks and the schema guard: before any comparison, a pre-check of stored table pairs, or of two schemas' tables paired by name, counts and adds up each side inside one snapshot (rows, null and repeated keys, per column null counts, empties, lengths, sums, minima, maxima, true and distinct counts) and says where to look first, worst first, as verdicts, never as values, a percentage or parity: agreeing figures can hide different rows, it is no run and no evidence, and a pair of two schemas can be drafted into a starter spec; the schema guard pauses a run whose table changed under its plan, creates paused a run that would compare less than its version's last run, warns of any other change and fails no run ([PRECHECKS.md](docs/mvp/PRECHECKS.md)). Measured once each on one Mac that was not idle, both PostgreSQL sides on one stock container ([PRECHECKS.md](docs/mvp/PRECHECKS.md) §12): 1M rows a side of 6 compared columns at level `columns` in 0.2 s between two PostgreSQL tables and 0.8 s, 1.5 s and 0.9 s between PostgreSQL and MySQL, PostgreSQL and Oracle, MySQL and Oracle; 10M rows a side in 1.7 s, and with distinct counts in 18.6 s and up to 1,940 MiB of temporary files; 300 pairs at level `schema` in 16.9 s, one session a target; the guard's look 28 to 81 ms. Not measured: MySQL or Oracle at 10M rows, distinct counts on them, a database on another host;
- cron schedules: a stored spec starts a run at wall-clock times of a 5-field cron expression in an IANA time zone, pinned to a version or following the name's newest; the worker starts each run under the same four-eyes gate and sampling rules (and, in a vendor build, licence) as a hand-started one, at most one per due time and never a backlog, and every time that started no run is recorded with a closed reason and can alert; not a dead-man's switch ([SCHEDULES.md](docs/mvp/SCHEDULES.md));
- signed releases: an image pinned and hardened, with its SBOMs, scan and triage, verified with OpenSSL before `docker load` ([RELEASE.md](docs/mvp/RELEASE.md)); a vulnerability process ([SECURITY.md](SECURITY.md)) and a pen-test scope ([PENTEST.md](docs/mvp/PENTEST.md));
- the benchmark harness (`tools/bench.py`, not in the image): synthetic tables in a throwaway PostgreSQL, `ds run` gated on exact ground truth; 0.1M to 10M rows a side published, observed on one Mac (an Apple M3 Max, synthetic data, warm cache), 100M one command on a server, not yet run ([BENCHMARK.md](docs/mvp/BENCHMARK.md) Results).

The design is in [docs/architecture.md](docs/architecture.md); the MVP plan and module contracts are
in [docs/mvp/PLAN.md](docs/mvp/PLAN.md) and [docs/mvp/CONTRACTS.md](docs/mvp/CONTRACTS.md).

## Quickstart

With Docker, one command in a checkout gives a running one-machine installation at
`http://localhost:8000`, with the mock services and a first API token saved to a file. It builds an
unsigned image from the checkout, and downloads three base images and the packages on first use:

```bash
sh deploy/up.sh
```

New here? Start with the guide, [docs/guide](docs/guide/README.md), which goes one step at a time:
[chapter 1](docs/guide/01-install-docker.md) walks through that command and the sign-in, its appendix
[A1-install-by-hand.md](docs/guide/A1-install-by-hand.md) takes the same steps by hand, and
[chapter 2](docs/guide/02-run-from-source.md) runs DeltaSleuth from the source, without Docker.
[docs/mvp/FIRSTRUN.md](docs/mvp/FIRSTRUN.md) is the script's contract. In a source checkout:

```bash
uv sync                                  # Python 3.13, installs the `ds` command
uv run pytest -q                         # unit, golden, property and end-to-end tests (Docker for the databases:
                                         # local postgres:16-alpine, mysql:8.4 and gvenzl/oracle-free images, never
                                         # pulled; without Docker their tests skip, unless DS_REQUIRE_DB=1)
                                         # On macOS the file reader's real faults are not made: a process
                                         # that ends by SIGSEGV or SIGBUS is a crash report and a dialog
                                         # there. DS_FAULTS=1 makes them (17 dialogs a run); on Linux they
                                         # are made always, and DS_IMAGE=<image> runs them in a built image
uv run ruff check deltasleuth mocks tests examples tools
(cd web && npm ci && npm test && npm run build)  # the UI, Node 22.12+ (served by `ds serve` from web/dist)
```

With a settings file (`--config` or `DS_CONFIG`) and a PostgreSQL 16 database it points at: a checkout's
settings file is a copy of `deploy/config/deltasleuth.dev.example.yaml` (four settings, no `auth` block), and
[docs/guide/02-run-from-source.md](docs/guide/02-run-from-source.md) sets up both, step by step
(`deploy/config/deltasleuth.example.yaml` is the Docker installation's file, and does not work on a
workstation):

```bash
cp deploy/config/deltasleuth.dev.example.yaml .deltasleuth/deltasleuth.yaml   # beside its db_dsn file
export DS_CONFIG="$PWD/.deltasleuth/deltasleuth.yaml"
uv run ds init && uv run ds migrate
uv run python -m mocks.fixtures linker --out demo/linker-samples
uv run ds run mocks/linker/spec.yaml --samples demo/linker-samples
uv run python -m mocks.run all           # in a second terminal: the four mock services, for live runs
uv run ds serve                          # the API (/v1), the UI (/) and the worker, on 127.0.0.1:8000
```

Without the `auth` settings `ds serve` runs in developer mode: loopback only, and every local user
acts as admin. Beyond loopback it needs them. People then sign in through the site's OIDC identity
provider, with six roles from their groups, machines use named API tokens, and every audit row names
a person or a token ([AUTH.md](docs/mvp/AUTH.md)); CLI commands that write audit rows need
`DS_OPERATOR`.

With an `mcp` block in the settings (`data_classes` is required), `uv run ds mcp check` prints what an
agent of your own would be offered and what MCP is not, and `uv run ds mcp` serves the tools over stdin
and stdout to the client that starts it ([runbook](docs/mvp/RUNBOOK.md#your-own-agent-mcp)).

No licence is needed. This build embeds no vendor key, so it is an open build: it enforces no licence
and limits nothing. Any number of pairs (spec names) run, and runs under SSO, four-eyes sign-off and
evidence packs ask for no licence (each keeps its own settings and rules). `ds licence` and the Licence
screen say so and show the installation id; `ds licence install` refuses every file, having no key to
verify one with. The licence mechanism (pairs, tiers, an end date, the labelled "unlicensed evaluation"
of two pairs) stays in the code for a vendor build that embeds keys, and applies only there
([LICENCE.md](docs/mvp/LICENCE.md) §19).

Live runs, the one-shot compare, measurements, adjudication and the reports are in the
[runbook](docs/mvp/RUNBOOK.md#try-it-on-the-mock-pairs); the office deployment (installing a signed
release, Docker Compose, secrets, sign-in, an OpenAI-compatible gateway, the CLI in a container) is in
[docs/mvp/DEPLOY.md](docs/mvp/DEPLOY.md). The office never builds the image: Compose runs the release it
loaded, and a source build is unsigned (DEPLOY section 2). The maintainer builds, scans, signs and tags a
release with `uv run python tools/release.py build …` ([RELEASE.md](docs/mvp/RELEASE.md) §9, whose §10
lists what comes first).

## Layout

| Path | What |
|---|---|
| `deltasleuth/core/` | Shared types, the spec loader, canonicalisation and diff, classification and root causes |
| `deltasleuth/connectors/`, `deltasleuth/evidence/` | Live REST, table (PostgreSQL, MySQL, Oracle and CSV or Parquet files behind one engine seam) and file-sample connectors, the evidence store |
| `deltasleuth/store/`, `deltasleuth/runner/` | The database (migrations, queries) and the runs: key sets, the worker, rechecks, stop conditions, cycles |
| `deltasleuth/reports/` | CSV exports, the PDF run report, the P0 tools (measure, adjudicate, summary), evidence packs, labelled accuracy and the readiness reports |
| `deltasleuth/agent/`, `deltasleuth/llm/` | The chat agent (tools, citation validator) over one port to any configured LLM |
| `deltasleuth/mcp/` | The MCP server for a team's own agent: the tools with their checks, caps and audit, and the MCP library's server over them (HTTP route in `deltasleuth/api/routes/mcp.py`, stdio in `ds mcp`) |
| `deltasleuth/api/`, `web/`, `deltasleuth/cli/` | The public API v1, the React UI and the `ds` command |
| `deltasleuth/auth/` | Principals and roles, API tokens, sessions and OIDC sign-in |
| `deltasleuth/notifications.py`, `deltasleuth/runner/notify.py`, `deltasleuth/core/notify.py`, `deltasleuth/connectors/notify.py` | Alerts and notifications: what people see and do, the worker's notifier, the events and bodies, one guarded send |
| `deltasleuth/readiness.py`, `deltasleuth/runner/readiness.py`, `deltasleuth/core/readiness.py` | Cut-over readiness: the evaluation every surface shares, the worker's measures and recorded transitions, and the pure gates, ladder and texts |
| `deltasleuth/prechecks.py`, `deltasleuth/runner/precheck.py`, `deltasleuth/runner/drift.py`, `deltasleuth/core/precheck.py` | Table pre-checks and the schema guard: what people make and see (the requests, the item, a pair, the draft), the worker's reads, the guard's looks and lines, and the pure figures, verdicts, changes and texts |
| `deltasleuth/schedules.py`, `deltasleuth/runner/schedule.py`, `deltasleuth/core/cron.py` | Schedules: what people declare and see (the writes, the banners, every firing's text), the worker's firing of due times, and the pure cron and time-zone rules |
| `deltasleuth/licence.py`, `tools/dsv.py` | Licence keys and entitlements, enforced only by a vendor build that embeds keys (this build embeds none and enforces no licence); the maintainer's offline tool that makes keys and issues licences (not in the image) |
| `tools/release.py`, `SECURITY.md` | The maintainer's release tool (build, SBOM, scan, gate, sign, tag), with its key list, triage and verification procedure in `tools/` (not in the image); the vulnerability process, which ships in every release |
| `tools/bench.py` | The benchmark harness: a throwaway `postgres:16-alpine` or a dedicated server, deterministic tables, `ds run` gated on exact ground truth, result files in `docs/bench/` (not in the image) |
| `deltasleuth/profiler/`, `deltasleuth/security/` | Draft specs from paired samples; tokenisation and masking |
| `deploy/`, `examples/` | Dockerfile (bases pinned by digest), Compose with its site example and source and CA overrides, and config examples; reviewed public API pairs, a demo pair of tables with the MySQL, Oracle and files acceptance kits, and a graph parity demo |
| `mocks/` | Two black-box mock service pairs with seeded discrepancies and ground truth |
| `tests/` | Unit tests per module, golden cases, property tests, end-to-end tests on the mocks |

## The runbook

Everything a team needs to onboard a service pair (what to send, the commands, how to review the
draft spec and the adjudication sheet, the exit criteria, troubleshooting) is in
[docs/mvp/RUNBOOK.md](docs/mvp/RUNBOOK.md).

## Licence

DeltaSleuth is licensed under the Apache License, Version 2.0: see [LICENSE](LICENSE).
