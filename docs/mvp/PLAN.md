# DeltaSleuth: design rules, what is built and what is not

DeltaSleuth is an end-to-end, deployable product. It is built and tested only with public endpoints,
mock services and mock LLM servers; nothing depends on a specific endpoint or message format.

## Design rules

1. **Minimal and extendable.** The simplest design that works. Extension happens through small
   seams: the connector protocol, the LLM dialect registry, domain packs and report formats. It
   never happens through configuration switches for hypothetical needs.
2. **Black-box.** Only endpoints and their declared, observable behaviour. Never how a service
   is built.
3. **Generic.** Any JSON service pair: REST GET/POST, GraphQL over POST, any shape.

## Built

- **Engine:** spec, canonicalise, diff, classify, root causes. A response whose not found is signalled
  by a 2xx body (GraphQL `{"data": {"x": null}}`) is ABSENT only when both the spec and the target
  match: a side's `notFound.status` may list 2xx statuses only together with `notFound.when`, and the
  target's `authoritative_absence` may list 2xx statuses only together with `shapes`.
- **Sources:** REST, sample files, and PostgreSQL tables and views
  ([TABLES.md](TABLES.md)): the scan of two tables, batched per-key reads and one-shot compares. A
  table side pairs with a table or a service side, and may take a row filter (`table.where`), key
  maps (`keyMap` `identity`, `prefix_pad` or `regex`) and type maps (`typeMap`). MySQL and Oracle
  tables and views are sides too ([ENGINES.md](ENGINES.md)), in any pairing with each other, PostgreSQL
  and a service, behind one engine seam: observed on MySQL 8.4.11 and Oracle 23.26.3 only, without graph
  parity, `regex` key maps or `ILIKE` on those sides; `ds target check` is their pre-flight. CSV and
  Parquet files in a mounted directory are sides too ([FILES.md](FILES.md)), read in place by one
  DuckDB locked to the declared directories, in a child process of its own that a fault of DuckDB's ends
  alone ([READER.md](READER.md)): the files are pinned by hash when a run is
  created and a change that a later check sees fails the run, a CSV's dialect is declared in the approved spec, and its lines
  that are not rows are counted, never dropped. Observed with DuckDB 1.5.5 and files of its own writer;
  no object storage, graph parity, `regex` key maps, `ILIKE` or table pre-checks on a file side.
- **Bulk graph parity** ([GRAPH.md](GRAPH.md)): L1 (the set difference of link facts) and L2 (each
  group's members, and components that split, merge or rearrange) over two fact tables or views.
- **Store:** PostgreSQL (lean schema) plus a content-addressed evidence directory.
- **Runs:** one-shot, batch and soak, including a soak over a table scan (scheduled re-diffs). They
  checkpoint and resume (kill -9 safe), recheck, honour stop conditions, produce interim soak
  reports, and list outcome changes across cycles.
- **Key sources:** keys given with the run, spec `list`, spec `paginated_endpoint`, the scan of
  two tables, and a `hash_sample` over a frame: a table side's population (`table`), a side's complete
  key listing, or a `union` of one of each ([SAMPLING.md](SAMPLING.md)).
- **Sign-off sampling** ([SAMPLING.md](SAMPLING.md)): a population too large for a census is signed off on
  a stratified `bottom_k` sample the worker draws with a secret nonce and commits before any key is
  compared, pre-registered by a `sampling` approval; never-compared keys are retried, each sign-off
  stratum is judged on an exact one-sided 95% Clopper-Pearson rule when the cycle ends, a population's
  ledger of draws is closed with fix notes, and a signed-off sampled cycle gets an evidence pack.
- **Labelled accuracy** ([ACCURACY.md](ACCURACY.md)): for the discrepancies an ended cycle observed, an
  operator draws a uniform salted sample against a named truth source, a named person labels it offline
  (`A_RIGHT`, `B_RIGHT`, `BOTH_WRONG`, `BOTH_RIGHT`, `UNDECIDABLE`), and DeltaSleuth counts the labels with
  exact two-sided 95% Clopper-Pearson intervals, per root cause too; a sign-off attests the newest
  labelling a person labelled, and its evidence pack carries the labels without keys. The engine still
  never decides which side is correct, and no figure gates a sign-off. Usable for `business_code` keys or a
  locating path shown in clear until the per-key reveal exists.
- **Reports:**
  - discrepancies, retry and coverage as CSV;
  - a PDF with root causes first and the trend against the previous run, with AI text only in
    a labelled annex.
- **AI:** the `ds_llm` port (openai_chat for OpenAI, vLLM and any OpenAI-compatible gateway;
  anthropic_messages for Claude).
  - An agent with the tools `get_run_summary`, `get_findings`, `get_observations` and
    `compare_key`.
  - A citation validator.
  - Chat scoped to a run or a key.
  - With AI off, everything else still works.
- **The MCP server** ([MCP.md](MCP.md)): a site's own agent reads a run's findings by id over the Model
  Context Protocol: eleven tools (the chat's four, and reads of runs, root causes, readiness, schedules
  and table pre-checks), with the chat's masking, caps and ids, as a named API token whose roles are
  cut to viewer and operator. One server on two transports, stateless HTTP (`POST /v1/mcp`) and stdio
  (`ds mcp`, a workstation's); off unless the settings have an `mcp` block, which states the data
  classes results may carry and whether `compare_key`, its only work tool, is served. No run control.
  DeltaSleuth checks no answer an external agent writes, and a result leaves its control. The block
  and the cut hold for calls through `/v1/mcp`: the token is a general API token, with its own role
  on every other route.
- **Interfaces:**
  - a FastAPI public API v1;
  - a small React + TypeScript UI covering runs, run detail, key diff, specs and profiling,
    approvals (under SSO), the licence (in this build, that none is needed), notifications, readiness,
    schedules, table pre-checks, and chat;
  - the `ds` CLI.
- **Access** ([AUTH.md](AUTH.md)): OIDC sign-in, named API tokens, six roles checked by every route
  and agent tool, and an audit log that names a person or a named credential in every row.
- **Four-eyes approvals** ([APPROVALS.md](APPROVALS.md)): under SSO, a spec version that shows values in
  clear, suppresses findings or filters what is compared runs only once a non-author approved its content
  hash (a version that draws a sample too, [SAMPLING.md](SAMPLING.md)); an ended cycle is signed off as
  parity evidence by someone who neither wrote its spec, started its run nor requested the sign-off.
  Tokens and developer modes label their runs unapproved.
- **Evidence packs** ([PACK.md](PACK.md)): a signed-off cycle that censused side a, or sampled a frame
  holding side a ([SAMPLING.md](SAMPLING.md) §11), gets a signed ZIP
  (facts, masked CSVs, the adjudication, its audit segment, a certificate) that an auditor verifies with
  OpenSSL and POSIX tools against the site's key register; the audit log is hash-chained, checked by
  `ds audit verify [--siem]`.
- **Licence keys** ([LICENCE.md](LICENCE.md)): built, and switched off in this build (§19). A build that embeds no vendor key, as this one, is an open build: it
  needs no licence and enforces none, so pairs are unlimited and SSO runs, sign-off and evidence packs
  need no licence. The mechanism stays for a vendor build, which embeds keys: there a vendor-signed file
  binds a customer, an order, a tier, a
  pair count and at most a year to one installation (database), checked offline; the pairs and the end
  date are enforced, SSO runs, sign-off and evidence packs are Enterprise's, and an installation without
  one is a labelled evaluation of two pairs. Only new work is refused; reads, reports, purge and audit
  always work.
- **Alerts and notifications** ([ALERTS.md](ALERTS.md)): the audit hash chain is the outbox; the worker's
  notifier follows it from a cursor, matches rules declared in the settings and sends signed HTTPS
  webhooks, SMTP e-mail, Slack and Teams messages of ids, counts and closed codes, with retries, mutes and
  SSRF guards; a failing channel never touches a run.
- **Cut-over readiness** ([READINESS.md](READINESS.md)): the worker measures each ended batch or soak
  cycle once onto its tombstone; a pure evaluator turns the measures, sign-offs, packs, adjudications,
  labellings and draw ledgers into twelve named gates and a place on a seven-rung ladder per pair (a spec
  name), under gates set in the settings and floored at architecture §15's tiers; a dashboard groups
  pairs by programme and entity type, with each pair's trend across soak cycles, and each change is an
  audit row that notifications route. The only figure is `k of m gates`: never a probability, a forecast
  or sign-off evidence, and it gates no sign-off.
- **Schedules** ([SCHEDULES.md](SCHEDULES.md)): a stored spec, pinned or followed by name, starts a batch
  or soak run at the wall-clock times of a 5-field cron expression in an IANA time zone. The worker of
  `ds worker` or `ds serve` fires through `create_run` as `system:worker`, so the four-eyes gate, a
  vendor build's licence and a sample's rules are the run's own; the run, the schedule's advance and their audit rows
  commit together, one run at most per due time. Only the newest due time is started, inside its start
  window; every other is a `schedule.missed` audit row with a closed reason, which notifications
  route. No key, value or setting; not a dead-man's switch.
- **Table pre-checks and the schema guard** ([PRECHECKS.md](PRECHECKS.md)): before any per-key comparison,
  a table pre-check of stored table pairs, or of two schemas paired by name, reads each side's catalogue
  and a few aggregate statements in one snapshot of its engine (row counts, null and repeated keys, and
  per column null counts, empties, lengths, sums, minima, maxima, true and distinct counts) and says where
  to look first, worst first, as verdicts; no row, key, text or distinct value is fetched, a minimum and
  a maximum are compared in the worker's memory and never stored or shown, it is no run and no evidence,
  and a pair of two schemas can be drafted into a starter spec. The schema guard pauses a run whose table
  is no longer what its plan reads, creates paused a run that would compare less than its version's last
  baseline run (while none is one, its newest stored plan), and warns of any other change; it fails no
  run.
- **Onboarding:** the profiler, plus the P0 tools: measure, adjudicate/score and report.
- **Deployment:** a Dockerfile (bases pinned by digest) and docker-compose (postgres + app, hardened),
  config examples and an office deployment guide.
- **Signed releases** ([RELEASE.md](RELEASE.md)): the maintainer's `tools/release.py` builds the image from
  a git archive, catalogues it (CycloneDX and SPDX SBOMs), scans it with grype and pip-audit, gates the
  findings on dated triage (OpenVEX), signs the bundle's checksums with a release key and tags the
  commit; a site verifies the bundle with OpenSSL and `sha256sum` before `docker load`, air-gapped.
  Source builds stay possible, unsigned. `SECURITY.md` is the vulnerability process, and
  [PENTEST.md](PENTEST.md) the pen-test vendor's scope.
- **Examples:** public pairs (SWAPI swapi.dev vs swapi.tech; Rick and Morty REST vs GraphQL)
  plus the two mock pairs with ground truth, two PostgreSQL tables with seed SQL
  (`examples/tables`), the MySQL and Oracle acceptance kits (`examples/tables/mysql`,
  `examples/tables/oracle`), the files acceptance kit (`examples/tables/files`: a declared CSV dialect
  against Parquet), and two link-fact tables for graph parity (`examples/graph`).

## Not built (left as seams)

The spec loader refuses a spec feature of this list with code `unsupported` and a one-line reason,
except those the list says are accepted with an `ignored` warning.

- noise calibration and masks;
- graph mode (`canonical.mode: graph`, a graph projection of service responses);
- table sides beyond PostgreSQL, MySQL and Oracle tables and views and CSV and Parquet files: other
  database engines (SQL Server is one more primitive set and a driver to approve; MariaDB is refused),
  files in object storage (a bucket is synced into the declared directory today), fixed-width files, SQL templates and the
  `lookup` key map that needs them, a key column's `part`, `session` and `collation`;
- sql, csv and run_keys key sources (as sampling frames too); `table` and `union` outside a
  `hash_sample`; `sampling.mode: hash`;
- frozen and shared-watermark time;
- change feeds (L4);
- for the MCP server ([MCP.md](MCP.md) §17): run control (start, pause, resume, cancel), a findings
  cursor, reads of a cycle other than the current one, a pre-check's pairs, IdP-token bearers, a
  tool that checks an agent's answer, and a token kind confined to `/v1/mcp`;
- ABAC, idempotency keys and the generic outbox (notifications follow the audit hash chain instead);
- notification ticketing (Jira, ServiceNow), approval reminders and quiet hours ([ALERTS.md](ALERTS.md) §16);
- Kubernetes;
- the tokeniser service (a local key file is used instead);
- the inferred-schema drift guard for service sides: `inferredSchema` is accepted with an `ignored`
  warning, and the `SCHEMA_DRIFT` code and CSV column stay (always 0); `schemaDrift` acts on table sides
  (the schema guard, [PRECHECKS.md](PRECHECKS.md) §7), and `mode: pause` between two service sides is
  refused;
- table diff strategies: `compare.strategy` is accepted with an `ignored` warning (two tables are
  always scanned by row hash, [TABLES.md](TABLES.md) §5);
- raw-body views: `sides.*.rawClear` is accepted with an `ignored` warning (outputs show canonical
  documents only);
- for labelled accuracy ([ACCURACY.md](ACCURACY.md) §17): the per-key reveal that locates records of a
  tokenised key, double labelling, a truth connector, frame weighting and labelling in the browser;
- for cut-over readiness ([READINESS.md](READINESS.md) §18): a four-eyes policy table for the gates
  (they are the host administrator's settings), stopping converged soaks, forecasts, XLSX and streamed
  CSVs;
- for schedules ([SCHEDULES.md](SCHEDULES.md) §13, §16): a run-now button, per-schedule retry settings,
  blackout windows and a start date, step-up or four-eyes approval for saving one, the setter among a
  scheduled run's excluded approvers, readiness naming a schedule's next due time, and a heartbeat a
  monitor can read while no worker runs;
- for table pre-checks and the schema guard ([PRECHECKS.md](PRECHECKS.md) §14, §16): a schedule kind for
  pre-checks, a rename map for pairs of two schemas, a readiness gate over the guard's record, a second
  person for an accept, shape hashes pinned in the approved spec, and bucketed counts to localise a
  difference.
