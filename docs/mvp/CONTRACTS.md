# Module contracts (binding)

The design rules of `docs/mvp/PLAN.md` apply to everything here. This file fixes the seams between
modules. Inside a module, the design is the simplest that meets
its contract.

## 1. Package layout

```
deltasleuth/
  core/ spec/ security/ connectors/ evidence/ profiler/ llm/   # core/sampling.py: a hash sample's selection and exact bounds (docs/mvp/SAMPLING.md); core/notify.py (events, rules, texts, bodies) and connectors/notify.py (one guarded send): docs/mvp/ALERTS.md; core/readiness.py (a cycle's measures, the gates, the ladder and every gate text, pure): docs/mvp/READINESS.md; core/cron.py (5-field cron in a time zone, pure): docs/mvp/SCHEDULES.md §4; core/precheck.py (a table pre-check's figures, verdicts and every pair text, and the schema guard's shapes, changes and verdict, pure; it names no engine): docs/mvp/PRECHECKS.md; connectors/tables/ (the engine seam of a table side, the column plan, the per-key connector, and MySQL and Oracle over one statement composer and one canonicaliser; figures.py, what a table pre-check reads of one side; manifest.py, duck.py and reader.py, the files engine: a files target's listing, pins and thread pool; the handle and cursors of the file reader, a child process; and that process itself, which alone loads the locked DuckDB that reads CSV and Parquet, with the frames the two speak: docs/mvp/FILES.md, docs/mvp/READER.md) with connectors/postgres/ (PostgreSQL's sessions, statements and adapter): docs/mvp/TABLES.md, docs/mvp/ENGINES.md; profiler/tables.py (a starter spec of two tables paired by name): docs/mvp/PRECHECKS.md §3
  config.py        # Settings, loaded from deltasleuth.yaml
  store/           # PostgreSQL: migrations/*.sql, db.py (pool, migrate), repo.py (queries), chain.py (the audit hash chain's checks)
  runner/          # keys.py, compare.py (one key), recheck.py, worker.py, runs.py, stop.py, counters.py, scan.py, graph.py, census.py, sample.py (draws, retries, judgements, the ledger: docs/mvp/SAMPLING.md), notify.py (the worker's notifier: docs/mvp/ALERTS.md), readiness.py (the worker's measures and recorded transitions: docs/mvp/READINESS.md), schedule.py (firing a schedule's due times, and rezoning: docs/mvp/SCHEDULES.md §5), precheck.py (reading a table pre-check's pairs: docs/mvp/PRECHECKS.md §5), drift.py (the schema guard: its looks V1 to V5, its record and every line it prints: docs/mvp/PRECHECKS.md §7)
  reports/         # csv.py, pdf.py, summary.py, measure.py, adjudicate.py (the P0 tools, DB-backed), pack.py and pack_verify.py (evidence packs: docs/mvp/PACK.md), accuracy.py (labelled accuracy: docs/mvp/ACCURACY.md), readiness.py (the dashboard and trends reports, a run report's section and readiness.csv: docs/mvp/READINESS.md §11), precheck.py (a table pre-check's precheck.csv, precheck-columns.csv and precheck.pdf: docs/mvp/PRECHECKS.md §11)
  agent/           # tools.py, loop.py, validate.py, prompt.py
  mcp/             # tools.py (the eleven tools an external agent reads through, with their checks, caps and audit; it imports nothing of the MCP library), server.py (the library's server over them, stdio and the self-test): docs/mvp/MCP.md (route: api/routes/mcp.py)
  auth/            # principal.py (roles), tokens.py, sessions.py, oidc.py: docs/mvp/AUTH.md
  approvals.py     # four-eyes approvals and sign-off: docs/mvp/APPROVALS.md
  licence.py       # licence keys and entitlements, enforced only in a build that embeds vendor keys (this one embeds none: an open build): docs/mvp/LICENCE.md
  notifications.py # the channels' state and banners, test sends, retries, mutes and a cycle's digest, for the API and CLI: docs/mvp/ALERTS.md §11
  readiness.py     # the evaluation of every pair, a pair's detail and trend, the transitions, check and verify, for the API, the CLI, the reports and the worker: docs/mvp/READINESS.md §10
  schedules.py     # a schedule's writes (create, replace, pause, resume, delete), its item, the banners and every firing's text, for the API, the CLI and the worker: docs/mvp/SCHEDULES.md §3, §9, §10 (routes: api/routes/schedules.py)
  targets.py       # the targets declared in the product: both lists loaded, a service declared, changed and removed without a restart, its credentials' files: docs/mvp/DEPLOY.md §7
  prechecks.py     # a table pre-check's creation (of stored specs, or of two schemas paired by name), its item and pairs, a pair, the cancel, the banners and a pair's draft, for the API and the CLI: docs/mvp/PRECHECKS.md §3, §10, §11 (routes: api/routes/prechecks.py)
  api/             # app.py (create_app) + routes/*.py; serves web/dist at /
  cli/             # main.py (Typer "ds")
tools/             # dsv.py, the maintainer's licence tool (LICENCE.md §14), release.py, the maintainer's release tool, with release-keys.txt, triage.toml and release-procedure.txt (RELEASE.md), and bench.py, the benchmark harness (BENCHMARK.md), whose result files sit in docs/bench/: outside the package and the image
web/               # React + TypeScript + Vite UI; build output web/dist
deploy/            # Dockerfile, uv-requirements.txt, docker-compose.yml, its site example and source and CA overrides, config/*.example.yaml
examples/public/   # swapi and rickandmorty specs, targets and keys (+ capture.py)
examples/tables/   # two PostgreSQL tables: spec, targets and seed SQL; mysql/ and oracle/: the acceptance kits (seed SQL, targets, a spec and the expected result)
examples/graph/    # two link-fact tables for graph parity: spec, targets and seed SQL
SECURITY.md        # the vulnerability process, the release keys and how a site verifies and scans a release (RELEASE.md §11); the pen-test scope is docs/mvp/PENTEST.md
```

`runner/` and `reports/` hold:
- recheck rules (§8.3);
- counters and their reconciliation;
- stop conditions;
- politeness (per-target gates, delay and jitter);
- masking of every output;
- OWASP CSV escaping;
- measure;
- adjudicate/score;
- the P0 report.

## 2. Settings (`deltasleuth/config.py`)

The YAML file path comes from `--config` or the `DS_CONFIG` environment variable (a path, never a
credential):

```yaml
database_dsn_ref: file:/run/secrets/db_dsn        # DSN contains the password -> file ref
evidence_dir: /var/lib/deltasleuth/evidence       # encrypted blobs
workspace_dir: /var/lib/deltasleuth/workspace      # without encryption: tokeniser.key and kek (`ds init`)
targets_file: /etc/deltasleuth/targets.yaml
llm_profiles_file: /etc/deltasleuth/llm_profiles.yaml   # optional: AI off when absent
llm_profile: gateway                               # default chat profile
llm_environment: prod
llm_external_data_classes: []                     # operator approval for external LLM hosts (dev/test/eval only)
auth:                                             # absent: developer mode, loopback only (docs/mvp/AUTH.md §8)
  public_url: https://deltasleuth.bank.example     # what people open, through the TLS proxy
  trusted_proxies: [172.30.0.1/32]                 # peers whose X-Forwarded-For is resolved
  oidc:                                           # absent: tokens mode (CI and trials; a vendor build's Community and Pro)
    issuer: https://login.microsoftonline.com/<tenant-id>/v2.0
    client_id: <application-client-id>
    client_secret_ref: file:/run/secrets/oidc_client_secret
    subject_claim: oid
    group_claims: [roles]
    roles: {viewer: [DS.Viewer], operator: [DS.Operator], admin: [DS.Admin]}   # immutable IdP ids
encryption:                                       # required with auth (docs/mvp/RETENTION.md §8)
  kek_ref: file:/run/secrets/kek                   # the keyring: one KEK per line, the last current
  tokeniser_key_ref: file:/run/secrets/tokeniser   # never inside evidence_dir or workspace_dir
retention: {standard_days: 90, audit_days: 365}    # 7..3650 each; audit_days >= standard_days
packs:                                            # needs auth.oidc; absent: the pack routes answer 503 (docs/mvp/PACK.md §3.1)
  installation: prod-eu-1                          # the name the site's key register lists
  signing_key_ref: file:/run/secrets/pack_signing_key   # PEM PKCS#8, EC P-256, unencrypted
worker: {concurrency: 4, lease_seconds: 120, poll_seconds: 1}
sampling: {temp_buffers: 2GB}                      # a draw's session memory for deduplicating a frame of two sources (docs/mvp/SAMPLING.md §3)
notify:                                           # absent: no notifier runs (docs/mvp/ALERTS.md §8)
  label: prod
  channels: {ops-hook: {kind: webhook, url: https://events.bank.example/deltasleuth, signing_key_refs: [file:/run/secrets/notify_hook_key]}}
  rules: [{name: run-health, events: [run.paused, run.stopped, run.failed, notify.dead], channels: [ops-hook]}]
readiness:                                        # absent: the contract's gates, and no key census for them (docs/mvp/READINESS.md §7)
  gates: {tier: standard, convergence: {cycles: 3}, fresh: {max_age: 7d}}   # the installation's; a programme or pair only tightens the tier
  programmes: [{name: cutover-q4, pairs: [{spec: customer-profile, entity: customer}]}]
precheck: {sessions_per_target: 1, statement_timeout_s: 900, distinct_max_rows: 20000000}   # the defaults (docs/mvp/PRECHECKS.md §5)
files: {memory: 2GB, spill: 20GB, threads: 2, roots_under: /var/lib/deltasleuth/files, max_files: 1000, plan_s: 300, os_limits: true}   # the defaults (docs/mvp/FILES.md §4, docs/mvp/READER.md §4); spill_dir: <evidence_dir>/spill
mcp:                                              # absent: no MCP server (docs/mvp/MCP.md §4)
  data_classes: [aggregates, production_tokenised] # required: what tool results may carry
  http: true                                       # POST /v1/mcp in ds serve (default false)
  work: []                                         # work tools served: compare_key
  calls_per_minute: 60                             # per caller, 1..600
  concurrency: 4                                   # calls at once per process, 1..16
```

Relative paths resolve against the settings file's folder, and so does every `file:` reference: under
each member whose name ends `_ref` or `_refs`, `file:<relative path>` names a file beside the settings,
whatever the working directory, while an absolute path stays and a bare `file:` is refused
([FIRSTRUN.md](FIRSTRUN.md) §5; the targets file's references follow the same rule).
`deploy/config/deltasleuth.example.yaml` is a one-machine trial as copied (tokens mode at
`http://localhost:8000`, AI off: a line that starts `# ` is a setting switched off), and
`deploy/config/deltasleuth.dev.example.yaml` is a source checkout's developer mode, its four paths
relative to its own folder (FIRSTRUN §2, §5).

`Settings.auth_mode` is `none` (developer mode), `tokens` or `oidc`. `api_token_ref` is refused with a
pointer to named API tokens. `encryption` is required with `auth`; without it the keys are the
workspace's `tokeniser.key` and `kek`, and with it a workspace `tokeniser.key` left over stops every
entry point (RETENTION.md §1). No setting, environment variable or file configures the licence: it is
stored in the database and verified against the vendor keys the build embeds ([LICENCE.md](LICENCE.md)
§4, §5). This build embeds none, so it is an open build, which needs no licence and enforces none; no
setting opens or closes a build either (§19). `readiness` (even `{}`) makes the worker count each key-list cycle's census as `packs` does;
its gates apply at restart, and `core/readiness.config_sha256` names them on every surface
([READINESS.md](READINESS.md) §7). `precheck` (`PrecheckSettings`, all defaulted, no secret) bounds table
pre-checks: the sessions the worker holds on a target at once to read them (1 to 4), the seconds each
of their statements may take (60 to 86,400) and the rows over which a side's distinct values and
duplicate keys are not counted (0, never, to 10^12) ([PRECHECKS.md](PRECHECKS.md) §5). `files`
(`FilesSettings`, all defaulted, no secret, applied at restart) is the file reader's budget in each
process that runs `ds`: DuckDB's memory and spill (sizes in binary units), its threads, the directory it
spills in, the directory every files target's root must lie under, a side's files at most, the seconds
a run's creation may spend planning its file sides, and whether the reader's process sets itself a data
limit and denies itself sockets on Linux (`os_limits`) ([FILES.md](FILES.md) §4, [READER.md](READER.md)
§4). No `ds` process imports DuckDB: a reader, a child process that alone loads it, is started at the
first file read, and never without a files target. `mcp` (`McpSettings`, no secret, applied at restart;
each `ds mcp` reads it at its start) turns the MCP server on: `data_classes`, required and without a
default, lists what tool results may carry (`aggregates`, `production_tokenised`, and MCP's own
`production_clear_declared`, which also serves the runs whose spec declares clear values or a
business-code key and needs `production_tokenised` beside it); `http` serves `POST /v1/mcp` in `ds serve`;
`work` lists the work tools served (`compare_key`, which needs `production_tokenised`);
`calls_per_minute` and `concurrency` bound a caller and the process ([MCP.md](MCP.md) §4). `ds serve`
imports the MCP library only while `mcp.http` is true.

`load_settings(path) -> Settings` is a frozen dataclass; the file is read like specs and targets
(`core.spec.documents`: a duplicate key is an error). `Context.open(settings)` holds these
long-lived objects:
- the db pool (`Context.open(…, pool=(min, max))` sizes it for `ds mcp`; unset, it is the process's own
  size, with `mcp.concurrency` more connections where `mcp.http` is true);
- the targets registry;
- the tokeniser;
- the keyring (`keyring`, `missing_keks` for the KEKs in use it lacks, and `kek`, the KEK new data
  keys are wrapped under), with `new_sealer(run_id)` for a new run's data key and `sealer(conn,
  run_id)` for an existing run's (cached; `KeyringError` while its KEK is missing):
  `security/keys.start` creates and loads the store's keys at every open, and in the server rewraps
  them under the current KEK (RETENTION.md §6.1);
- the evidence store, holding encrypted blobs under keyed names (RETENTION.md §2);
- the LLM profiles;
- `gates`: one politeness gate per target id, shared by the worker, `compare_one` and key listings: the
  target's rate budget, in-flight cap and `Retry-After` pause, and one circuit breaker for each
  `execution.circuitBreaker` (rate, window, cooldown) that a spec calling the target declares
  (`TargetGate.breaker`): a spec's own declaration governs its calls, the specs that declare the same one
  share its state, and a spec that declares none is never refused;
- `oidc`: the IdP client, which only the server builds, in oidc mode (`Context.open(settings,
  oidc=True)`); the worker and the CLI get `None` and never read the client credential;
- `signer`: the evidence packs' signing key (`security/signing.PackSigner`), which only the server
  loads (`Context.open(settings, packs=True)`), with `packs` set; else `None`.

It is the dependency passed to runner, reports, agent and api. `ctx.spec(conn, spec_id)` compiles a
stored spec without today's targets, so a removed target never hides a run; `live=True` also checks
them, and that the spec's `normalizerVersion` is this build's, only to create runs and to call
targets. `ctx.capabilities(run)` is what the run's targets
declared when it was created (`params.capabilities`), which every read that observes evidence
again uses.

## 3. Store (PostgreSQL 16+, schema `ds`)

Migrations are numbered SQL files applied by `ds migrate` / at startup; `ds.schema_version` records
them (`001` the schema, `002` due keys looked up per running run, `003` a table scan's state, `004` scans
per cycle and graph parity rows, `005` people, sessions, API tokens, creators and an attributable audit
log: [AUTH.md](AUTH.md) §7; `006` retention, the data keys and the spine on `key_masked`, and `007` the
end of plaintext storage: [RETENTION.md](RETENTION.md) §7; `008` four-eyes approvals and immutable spec
bodies: [APPROVALS.md](APPROVALS.md) §7; `009` the audit hash chain, censuses, adjudications and evidence
packs: [PACK.md](PACK.md) §8; `010` the installation id and its licences: [LICENCE.md](LICENCE.md) §13;
`011` sign-off sampling: [SAMPLING.md](SAMPLING.md) §12; `012` notifications: [ALERTS.md](ALERTS.md) §13;
`013` labelled accuracy: [ACCURACY.md](ACCURACY.md) §13; `014` cut-over readiness:
[READINESS.md](READINESS.md) §13; `015` schedules: [SCHEDULES.md](SCHEDULES.md) §12; `016` table pre-checks
and the schema guard's record: [PRECHECKS.md](PRECHECKS.md) §8; `017` the index a worker's lease renewal
reads, in place of every key of the store).
The tables are a lean subset of `docs/architecture/control-plane.sql`,
with the same names and meanings (`approvals` replaces its `spec_approvals`):

| Table | Key columns |
|---|---|
| `specs` | `spec_id` bigserial, `name`, `version`, `content_hash` unique, `body` jsonb, `created_by` (the first uploader), `created_at`; never updated or deleted (`specs_immutable`, 008) |
| `runs` | `run_id` text pk (`R-<ulid-ish>`), `spec_id`, `mode` one_shot/batch/soak, `status` queued/running/paused/cancelling/cancelled/completed/completed_with_issues/failed, `params` jsonb (keys source, soak, samples_dir, capabilities, tables, `approval: {needs, mode}` as created, plus `from_cycle` once a resume attached its first approval; from 010 `licence`, the licence it was made under: `{licence_id, tier, state, ends}` and `clock_behind`, or null for a run an open build made, LICENCE.md §10, §19), `cycle` int, `counters` jsonb, `stop_reason`, `error`, `created_by`, `retention` (standard/audit, from the spec), `hold_reason`, `hold_by`, `hold_at`, `approval_id` (the spec approval it started, resumed or attached under), `approval_kind` (always `spec`), `keys_cut` (`{resolved, taken}` when `maxKeys` cut the key set), `census` (the current cycle's key census), from 011 `draw` (a sampled run's pending draw, `{due[, error, at]}`) and a sample's `params.method` and `params.population`, from 016 `guard` (jsonb, nullable, no default: what the schema guard recorded of the run's tables, format 1, outside `params`; PRECHECKS.md §7), timestamps; `repo.get_run` and `list_runs` add `created_by_name` |
| `scans` | pk (`run_id`, `cycle`); `state` jsonb: a table scan's counters, resume point and, for graph parity, `graph` counts. `repo.get_run` and `list_runs` return the current cycle's as `scan` (null for other runs) |
| `graph_diffs` | pk (`run_id`, `cycle`, `seq`); `code`, `label` (masked), `count_a`, `count_b`, `only_a`, `only_b`, `detail` jsonb (masked samples): graph parity's L2 rows |
| `run_keys` | pk (`run_id`, `cycle`, `key_masked`); `key_enc` (the raw key, sealed), `seq`, `state` pending/leased/recheck/done/cancelled, `available_at`, `lease_until`, `lease_owner`, `pass_no`; from 011 a sampled key's `stratum`, `signoff_stratum`, `signoff_member` and `retries` (labels live here only, SAMPLING.md D12) |
| `observations` | `obs_id` pk, `run_id`, `cycle`, `key_masked`, `side`, `pass_no`, `http_status`, `content_hash` (`hk1:`, the blob's keyed address), `headers_enc` (sealed), `latency_ms`, `attempts`, `pages`, `pages_eof`, `pagination_unstable`, `error_class`, `observed_at` |
| `results` | pk (`run_id`, `cycle`, `key_masked`); `outcome`, `side_a_state`/`reason`, `side_b_state`/`reason`, `inconclusive_reason`, `drift_reason`, `confirmation`, `first_outcome`, `revision`, `suppressed_count`, `obs_a_id`, `obs_b_id`, `attrs` jsonb |
| `findings` | pk (`run_id`, `cycle`, `key_masked`, `seq`); `finding_id` text unique (`F-…`), `taxonomy_code`, `severity`, `change_type`, `direction`, `path_pattern`, `json_pointer`, `value_a`, `value_b`, `value_class`, `member_key_tok`, `signature_hash`, `rule_id`, `rule_classification`, `suppressed` |
| `root_causes` | pk (`run_id`, `cycle`, `root_cause_id`); `kind`, `taxonomy_code`, `max_severity`, `path_pattern`, `change_type`, `direction`, `affected_keys`, `finding_count`, `member_key_tok`, `sample_keys` |
| `chat_sessions` | `session_id` pk, `run_id`, `key` null, `created_by` (only this actor may read or continue it), `created_at` |
| `chat_messages` | pk (`session_id`, `seq`); `role`, `sealed` (`{content, tool_calls}`, sealed), `cycle`, `citations` jsonb, `created_at` |
| `llm_calls` | `call_id` pk, `session_id` (set null when its session is purged), `profile_id`, `model_reported`, `usage` jsonb, `cost`, `latency_ms`, `error`, `created_at` |
| `data_keys` | `scope` pk (`address`, `R-…` a run's, `E-…` an evidence key), `kek` (the `K-…` id of the KEK that wraps it), `wrapped`, `created_at`, `rewrapped_at` |
| `cycles` | pk (`run_id`, `cycle`), one per ended cycle and never deleted: `ended_at` (the retention clock), `counters`, `purging_at`, `purged_at`, `purged_by`, `purge_reason`, and the run's `approval_id` (null before `params.approval.from_cycle`), `keys_cut` and `census` (a key-list run's, PACK.md §1.2) as the cycle ended, a drawn cycle's `sample`, its judgement (SAMPLING.md §8.1), and `readiness`: the cycle's measures (format 1: counts, ids, codes and stratum labels), written once by the worker, or the marker of a one-shot or captured-sample cycle, which readiness leaves out (READINESS.md §4) |
| `sample_draws` | `draw_id` pk (`D-…`), (`run_id`, `cycle`) unique, `spec_id`, `method` bottom_k/rate, `population`, `salt_label`, `nonce` (unique), `drawn_at`, `keys`, `frame`, `strata`, `selection_sha256`, and once `closed_by` (a person)/`closed_at`/`close_note`; inserted as of `now()` for a running run's current cycle before its keys, closed once after its cycle ended, never edited, deleted or purged (SAMPLING.md §12) |
| `approvals` | `approval_id` pk (`A-<ulid-ish>`), `kind` spec/signoff, `spec_id`, `run_id` and `cycle` (a sign-off's), `scopes` (a spec version's needs), `attests` jsonb and `attests_sha256` (a sign-off's facts), `status` requested/approved/rejected/revoked, `requested_by`, `request_note`, `authors` and `unattributed` (set by the trigger), `first_by`/`first_at` (the first of two approvers), `decided_by`/`decided_at`/`decision_note`, `revoked_by`/`revoked_at`/`revoke_reason`; never deleted, and `ds.tg_approvals` checks every write (four-eyes, the decider's stored roles) |
| `adjudications` | `adjudication_id` pk (`J-…`), (`run_id`, `cycle`) of an ended cycle, `salt`, `sample_limit` 1–500, `selection`, `sheet`, `drawn_by`/`drawn_at`, and once scored `upload_sha256`, `sheet_sha256`, `score`, `score_sha256`, `passed`, `scored_by`/`scored_at`, `purged_at`; one score, no draw or score during a requested or approved sign-off, never deleted (PACK.md §5) |
| `labellings` | `labelling_id` pk (`V-…`), (`run_id`, `cycle`) of an ended cycle, `salt`, `size`, `per_cause`, `accepted`, `agreed`, `truth_source`, `truth_as_of`, `selection` (items, counts, causes, `uncovered`, `names_side`, `unlocatable`, warnings), `selection_sha256`, `sheet`, `drawn_by`/`drawn_at`, and once labelled `sheet_sha256`, `labels_sha256`, `accuracy` (the score), `score_sha256`, `labelled_by`/`labelled_at`, `purged_at` (no hash of the upload: it would hash the notes); labelled once, no draw or labels during a requested or approved sign-off, never deleted: a purge nulls `sheet` and `selection.items` only (ACCURACY.md §13) |
| `packs`, `pack_zips` | `pack_id` pk (`P-…`), (`run_id`, `cycle`), `signoff_id`, `adjudication_id`, `manifest`, `sums_sha256` unique, `zip_sha256`, `key_fingerprint`, `audit_seq`, `issued_by`/`issued_at`, `status` issued/revoked with `revoked_by`/`revoked_at`/`revoke_reason` (copied from the sign-off, whose leaving `approved` revokes its packs), `purged_at`; the ZIP in `pack_zips`, deleted only by its cycle's purge (PACK.md §6) |
| `audit_chain`, `audit_chain_upgrade` | `seq` pk, `id` (the audit row), `object`, `prev`, `obj_prev`, `hash`, `text`: one entry per audit row, written by a trigger for every writer; `upgrade_seq`, the rows 009 chained (PACK.md §4) |
| `installation` | `installation_id` pk (`I-` and 16 hex), `created_at`: one row, written once by 010 from the first `audit_chain` row (random on a store without one), kept by a dump and restore; append-only (LICENCE.md §5) |
| `licences` | `licence_id` pk (`LI-…`), `text` (the normalised licence file), `sha256` unique, `installed_by`/`installed_at`, `withdrawn_by`/`withdrawn_at`/`withdraw_reason` (once, as of `now()`); never deleted, re-verified at every read, so a row written with SQL grants nothing (LICENCE.md §5, §13) |
| `schedules` | `schedule_id` pk (`SC-…`), `name` (a slug, unique among those not deleted), `spec_name`, `spec_id` (pinned; null: the name's newest version at each firing), `run` jsonb (`{mode, soak?}`, never keys), `cron`, `time_zone`, `start_within` 15–1440, `ends_at`, `auth_mode`, `revision`, `created_by`/`created_at`, `set_by`/`set_at`, `paused_by`/`paused_at`, `deleted_by`/`deleted_at`, `anchor_at`, `next_due` (null: paused, ended or deleted), `last` jsonb and `last_at` (its newest settlement), `last_run_id`, `held` jsonb (a standing refusal's range not yet chained); never deleted (SCHEDULES.md §12) |
| `prechecks`, `precheck_pairs` | a table pre-check (`precheck_id` pk `PC-…`, `source` specs/catalogue, `level` schema/counts/columns/distinct, `request` jsonb (spec ids, target ids, schemas, globs), `pairs` 1–500, `skipped`, `evaluation`, `created_by`/`created_at`, `cancelled_by`/`cancelled_at`, `finished_at`); each of its pairs (pk (`precheck_id`, `n`), `spec_id` (null: paired by name), `label`, `target_a`/`relation_a`, `target_b`/`relation_b`, `status` pending/running/done/cancelled, `tries`, `claimed_at` (when a worker took it, renewed while it reads it), and once read `verdict`, `rank`, `differing`, `rows_a`, `rows_b`, `line` and `result` jsonb (format 1: counts, verdicts, closed codes, names and declared types; never a minimum, maximum, distinct or key value), `finished_at`), cascading with its pre-check; deleted `retention.standard_days` after the pre-check was made (PRECHECKS.md §8) |
| `notify_cursor`, `notify_deliveries`, `notify_mutes` | the feed's cursor, one row: the last audit chain `seq` turned into deliveries; a delivery per event and channel (`delivery_id` pk `N-…`, unique (`event_id`, `channel`), `event`, `severity`, `rules`, `dedup_key`, `run_id`, the rendered `body` and its `body_sha256`, `render` (the channel's `kind:labels:links` it was rendered for), `state` pending/sent/dead/suppressed with `why`, `attempts`, `due_at`, `last_error`, `last_status`); a mute (`mute_id` pk `M-…`, `scope` channel/run, `target`, `until` at most 7 days on, `reason`, ended once as of `now()`). No foreign key: 012 touches no existing table (ALERTS.md §13) |
| `blob_gc` | `content_hash` pk, `queued_at`: blobs whose observations were purged, deleted 24 h later unless named again |
| `audit_log` | `id` bigserial, `at`, `actor` (`user:U-…`, `token:T-…`, `local:<operator>@<host>`, `system:…`, `dev:loopback`; older rows keep `api` or `ds`), `actor_name`, `action`, `object`, `detail` jsonb, `request_id`, `session_id`, `client_addr` (append-only) |
| `users` | `user_id` pk (`U-…`), unique (`issuer`, `subject`), `username`, `display_name`, `roles` (at the last sign-in), `last_login_at`, `disabled_at`, `disabled_by` |
| `sessions` | `session_id` pk (`W-…`), `secret_hash` (sha256 of the cookie secret), `user_id` or `token_id`, `actor`, `name`, `roles`, `roles_sha256`, `client_addr`, `auth_time`, `acr`, `amr`, `last_seen_at`, `expires_at`, `revoked_at` |
| `api_tokens` | `token_id` pk (`T-…`), `name`, `owner`, `owner_user_id`, `role` (viewer, operator or spec_author), `secret_hash` (sha256 of the token), `created_by`, `expires_at`, `last_used_at`, `revoked_at` |

`created_by` is an actor, NULL on rows from before 005; `ds.actor_name(actor)` gives a user's or
token's name.

Rules for the store:
- **Findings:** stored values are masked (tokens or cleared values), never raw payload. A recheck's
  revision keeps a finding's `F-` id when its masked `json_pointer` and `change_type` are unchanged;
  a new finding gets a new id, and a finding that disappears takes its id with it.
- **Evidence:** bodies live only in the evidence dir, as blobs encrypted with AES-256-GCM under a
  data key per blob, named by an HMAC of the body (no other file); observation metadata lives in
  `ds.observations` (RETENTION.md §2).
- **Values PostgreSQL cannot store** (NUL) are stored as the text `\u0000` (`repo.storable`), in
  jsonb, finding and root-cause columns and chat messages: a deterministic data error is never
  retried in a loop.
- **Raw values** are sealed with AES-256-GCM under the run's data key: the raw key in
  `run_keys.key_enc` (the fetch needs it), a table scan's resume point in `scans.state.after`,
  headers in `observations.headers_enc` and chat in `chat_messages.sealed`. Repo functions return the
  sealed columns, and `sealer.open_row` opens them; every other table joins on `key_masked`, and
  everything else displays keys through the masker (RETENTION.md §3).
- **Retention:** each ended cycle gets a `ds.cycles` row (`repo.end_cycle`); its data is deleted once
  it ended longer ago than its run's retention (RETENTION.md §4, §5).
- **`repo.py`:** plain SQL, no ORM. It has one function per use case, and every function takes a
  connection or pool.
- **Approvals:** under SSO a run of a spec version whose content needs one starts, resumes and attaches
  only under an active approval of its content hash (`approvals.gate`, APPROVALS.md §4); a revoke
  pauses or stops what rests on it in one transaction.
- **Sample draws** (SAMPLING.md §5, §9): a live `bottom_k` run per population, checked under the advisory
  locks DSSM of its population ids; a population's ledger is ordered by the chain seqs of its
  `sample.draw` and `sample.close` audit rows.
- **Licence** (LICENCE.md §5, §7): `licence.standing` reads the rows, the installation id and the pairs
  counted in the licence's window at each decision; new pairs and installs serialise on the advisory
  lock DSLC. In an open build (§19) it reads the id and the rows, judges none, counts no pair and takes
  no lock: its standing is `open`.
- **Notifications** (ALERTS.md §4, §5): the audit hash chain is the outbox. The notifier reads chain rows
  after its cursor and moves it in the transaction that stores their deliveries, so each row is turned
  into deliveries once; deliveries and mutes go `retention.standard_days` after they were made, ended or
  lapsed.
- **Labellings** (ACCURACY.md §3, §5): `repo.labelling_draw` draws a labelled sample in one statement
  under a 300 s statement timeout, with hash and merge joins only (`enable_nestloop` off, so its cost does
  not depend on the planner's estimates); `repo.replace_root_causes` analyses the findings' `root_cause_id`
  when the statistics hold under half the findings it linked; `repo.list_labellings` reads no sheet,
  drawn items or per-cause and per-rule figures unless asked for full rows; `ds.tg_labellings` holds a
  labelling to one upload of labels and one purge, and refuses a draw or labels while the cycle has a
  requested or approved sign-off, judged under the cycle's row lock.
- **Readiness** (READINESS.md §4, §8, §14): `ds.cycles.readiness` holds each ended cycle's measures,
  written once (`repo.set_readiness`, over none or a failed try's) and kept with the tombstone;
  `repo.end_cycle` marks a one-shot or captured-sample cycle excluded. `repo.readiness_series` and
  `repo.readiness_governance` read what every pair is judged from in one statement each, on the partial
  indexes of 014; `repo.cycle_changes` plans without nested loops, like the labelling draw. A record
  whose `format` is not the build's is an unknown point, and each pair is evaluated on its own: one whose
  measures cannot be read reads `unknown`, and the others are judged. A pair's recorded transitions are
  audit rows (`readiness.changed`), read through `audit_log_object_idx`; no readiness table exists.
- **Schedules** (SCHEDULES.md §5, §12): `ds.schedules` is written by `repo`'s schedules section only
  (plain SQL; a person's write locks the row, a firing advances it by compare-and-set on `next_due` and
  `revision`, and a create counts the schedules under the advisory lock `DSSC`, so no more than 200
  are ever stored). A scheduled run's `params.schedule` is `{schedule_id, due, revision, time_zone}`, and the
  unique index `runs_schedule_due_idx` on `ds.runs` refuses a second run of one schedule and due time
  from any writer; the `runs_schedule` CHECK (`NOT VALID`, so 015 rewrites nothing) holds `due` to its
  canonical text. The firing history is audit rows (`schedule.fire`, `schedule.missed`,
  `schedule.rezone`), read through `audit_log_object_idx`; no firings table exists.
- **Table pre-checks** (PRECHECKS.md §5, §8): `ds.prechecks` and `ds.precheck_pairs` are written by
  `repo`'s pre-checks section only (plain SQL). A create counts the unfinished pre-checks under the
  advisory lock `DSPC` (20 at most); the worker claims a pending pair (`claim_pair`, counting a try and
  setting `claimed_at`), renews the claim while it reads it (`hold_pair`), sets it back to pending when it
  stops reading it (`requeue_prechecks`), and settles it with its result in one statement (`save_pair`, or `settle_target` for the other pending pairs
  of its target: of every unfinished pre-check when the credential was refused, of its own when the target
  stayed unreachable), each taking the pre-check's row lock
  so that `finish_precheck`, next in the same transaction, sees every pair settled. A list and a
  pre-check's pairs read no `result` (`precheck_pairs_worst_idx` orders them worst first). No run, cycle,
  pair of the licence or pack is written or read by a pre-check, and no evidence module reads either
  table. `runs.guard` is written only by the schema guard (`repo.set_guard`, under the run's row lock
  taken by `repo.held_guard`) and read by `repo.guard_basis` (the version's newest earlier baseline run;
  while none is one, the newest that stored a plan, not one the guard created paused that no person
  accepted; never a one-shot); `deps.shown` leaves it out of every run row the API shows, and
  `plan_sha256` hashes `params`, which it is not in.
- **Audit:** `repo.audited(pool)` is the one way to write audit rows. The insert is the last statement
  before commit, and each row's JSON line on the `deltasleuth.audit` logger (the SIEM copy, with its
  `chain` entry) is logged only after the commit (AUTH.md §6); `ds audit verify` checks the chain
  (PACK.md §4).

## 4. Runner

- **Who acts.** Every run action (`create_run`, `pause_run`, `resume_run`, `stop_run`, `cancel_run`,
  `compare_one`, and the cycle's end) takes a keyword-only `who: Principal`, which its audit rows
  name; the worker acts as `system:worker`. Role checks stay in the API (AUTH.md §4.3).
- **`create_run(ctx, spec_id, mode, keys=None, samples_dir=None, soak=None, *, who, schedule=None) -> run_id`**
  - It resolves the key set once. The sources, in order: explicit `keys`, then spec `key.source`
    `list`, then `paginated_endpoint` (fetched through the connector with the target's rate
    budget; a listing read incompletely, or repeating a member name, refuses the run), then a key
    table (`kind: table` with a `target` and a `table`, §11: read in one snapshot, each key once, at
    most 1,000,000; one that cannot be read, or holds a row without a key or, in a CSV file, a line
    that is not a row, refuses the run). A batch
    or soak over two tables with neither scans them instead (§11), a soak once per cycle.
  - Every key must match `key.pattern` after `key.normalize`, fit in 1024 UTF-8 bytes and hold no
    NUL; otherwise ValueError names the positions, never the keys.
  - A composite key beside a service side is a multi-part key. Its text is its parts in `key.parts`
    order joined by `|` (`\|` and `\\` for a `|` or `\` inside a part; a `\` before any other character,
    or as the last one, refuses the key by its position, as a wrong number of parts does), wherever a key
    is given or shown; each part is normalised and matched on its own (`core/spec/keys.fit_parts`,
    `parts_match`: one rule for inline lists, given keys, listings, key tables, sample names and a spec
    view's masked keys), and a request names a part as `{{ keyParts.<name> }}`, encoded as `{{ key }}` is. A
    listing's `keyPath` selects the parts (`$['region','shelf','code']`). Two tables' composite key is
    judged whole, as before.
  - The rule over the keys, their masking and their sealing run in a thread, here and at a soak's roll:
    a key set may hold a million keys.
  - It inserts the run, its keys and `params.capabilities` (what its targets declare now), and
    audits the call. No target of the product's list changes while it does (`targets.unmoved`), and one
    the spec names that was moved or removed since the spec was judged, while the keys were listed,
    refuses the run: ValueError `target <id> was changed or removed while the run was being created:
    start it again` (DEPLOY.md section 7).
  - A file side (FILES.md §6) is planned here, as in `compare_one`: its files listed, hashed and counted
    on the file reader's threads inside `files.plan_s` (one for all the run's file sides), and its pins
    sealed as `params.pinned`. An export that is not ready (absent, still being written, stale, unstamped,
    or a plan past `files.plan_s` or one the reader was too busy to take)
    raises `NotReady`, a ValueError holding a code, which a schedule's firing waits for (SCHEDULES.md §5).
  - `samples_dir` runs a batch in file mode (sample layout 1/2; no recheck); a soak refuses it,
    since captured samples do not change between cycles.
  - A `hash_sample` key source (SAMPLING.md §5) resolves no key: it refuses explicit keys (R4), a soak
    that does not roll (R2), file mode (R3) and a `bottom_k` spec whose population a live `bottom_k` run
    samples (R1, again under the DSSM locks), then inserts the run `running` with no keys and
    `draw = {due: now}`. The worker's draw task reads the frame (a table side's population in one
    snapshot, a side's complete key listing, or one of each), selects per stratum, and stores the draw,
    its labelled keys, the frame's census and `sample.draw` in one audited transaction; a frame's refusal
    (W1–W9) pauses the run (`run_paused_draw`). A sign-off member no read compared is read again from its
    first pass, at most `resolve.times`. Every end of a drawn cycle stores its judgement; a rolling
    soak's next cycle draws its own sample, paused (`run_paused_sample`) after a `bottom_k` draw that did
    not pass until that draw is closed (R5).
  - With `schedule` (a `Firing`: the schedule, its due time, the revision seen, the zone, who saved it,
    the window's end; SCHEDULES.md §5) the run records `params.schedule`, and its insert transaction
    advances the schedule (`repo.settle_schedule`, a compare-and-set that also refuses a commit after the
    window's end, by `clock_timestamp()`): zero rows raise `ScheduleMoved` and roll the run back, as
    `SetterGone` does when who saved it is by then disabled or no longer an operator. The audit rows
    come last, in order: a flushed held range, `create_run` (its detail gains `schedule`),
    `licence.pair` if any, `schedule.fire`. Without `schedule`, params and detail are unchanged.
  - With a table side (PRECHECKS.md §7, V1) it plans through `tables.planning`, reads the version's
    newest earlier baseline run, else its newest stored plan (`repo.guard_basis`), in its insert
    transaction and records the run's `guard` (each side's planned shape and what the plan reads each
    column as). A run that would not
    compare what that run compared is created `paused` (`run_paused_schema_drift`) before any row is
    read, or refused (`drift.Refused`, 422, nothing written) when its spec pauses on schema drift; any
    other change is a warning (`POST /v1/runs` answers `warnings`). Its audit rows end `paused` and
    `schema.drift` after `create_run`, `licence.pair` and `schedule.fire`. `compare_one` only warns.
- **Worker** (`ds worker`, also started inside `ds serve` unless `--no-worker`, which leaves the keys
  to a `ds worker` beside it: ENGINES.md §13):
  - One worker per database processes keys: it holds a session-level advisory lock, and another
    worker (for example `ds run` while `ds serve` runs) waits for it. A target's rate budget
    therefore holds across runs and the app's compares and key listings; a command-line
    `ds compare`, or the key listing of `ds run`, has its own process's budget.
  - It leases due keys (`FOR UPDATE SKIP LOCKED`, `lease_until`) of running runs, run by run, and
    processes them concurrently under the per-target gates (`ctx.gates`). No slot leases a key of a
    run whose `execution.concurrency` lanes are all busy. The lease is renewed while the key waits
    for a lane and while it is read, and a key is read only while its run is `running`.
  - For each key it reads A then B, stores evidence first, observes and compares, then persists
    the observations, result and findings in one transaction (between two tables, a group of up to
    200 keys: one query per side for its first reads and one transaction, TABLES.md §12).
  - A side whose answer was cut is INCOMPLETE `truncated`: its key is INCONCLUSIVE and never compared
    (`canonical/observe._paging_state`). What the key reads follows `recheck.outcomes`. Under the
    default, which holds INCONCLUSIVE, a cut key is read again (`times` more reads a side) and, cut
    again, reads `recheck_failed` with `api_uncomparable` 0, its side's reason still `truncated`. With
    `outcomes: [MISMATCH, NOT_FOUND_A, NOT_FOUND_B]` it is read once and is INCONCLUSIVE `truncated`, in
    `api_uncomparable` (as in a run from captured samples, which reads nothing again). The count rule
    comes first (the side's `truncation` count members, else its target's `truncation_indicators`), then
    the side's `truncation.flagPaths` on the parsed answer: a node that is `true` (`the truncation flag
    <path> is true`) or neither `true` nor `false` (`... is not a boolean`); `false`, or no node, detects
    nothing. Flags are the spec's alone: `params.capabilities` and `plan_sha256` hold none.
  - A discrepant key whose outcome is in the recheck outcomes goes to state `recheck` with
    `available_at = now + delay + jitter`. Its recheck pass reads B then A and applies `confirm`.
    A key found by a table scan is confirmed by its per-key read instead (§11).
  - An expired lease is re-leased, so a crash or kill -9 loses at most in-flight work and never
    duplicates results (upsert on the pk).
  - A live 401/403 pauses the run (`run_paused_auth_error`) and hands the key back unjudged; a
    resume clears that reason, reads the targets' credential files again, and the key is read
    again. In file mode a sample recording a 401/403 is judged like any failed read. Only a lost database connection hands a key
    back to be read again; any other error fails the run, but for a table read refused with a code of
    the denied class (`UndefinedColumn`, `UndefinedTable`, `InsufficientPrivilege`, `TargetDenied`): the
    schema guard looks at the run's tables first (V5, `drift.failed`), and a change that breaks the plan
    pauses the run (`run_paused_schema_drift`) instead, its keys handed back.
- **Cycle end** (all keys done or cancelled):
  - compute root causes and counters, which must reconcile;
  - set the status;
  - for soak, if no stop condition fired, insert the next cycle's keys with
    `available_at = cycle start + cycleInterval` (fixed key set, or re-resolved when
    `params.soak.rolling`, before the run's row lock; a failed listing keeps the previous keys and
    audits `rolling_resolve_failed`). A key table is read again there, inside `min(3600 s,
    cycleInterval)` (3600 s for an interval of zero), since the read holds the worker's sweep and a slot
    of its target's gate: a failed read keeps the previous keys too, its audit row holding no host or
    account, and a refused credential pauses the run (`run_paused_auth_error`) until a resume reads again.
  - One run whose cycle cannot end fails alone; the other runs' cycles still end.
- **Keys:** a run's raw keys are sealed under a data key created with the run and bound to its id
  (`ctx.new_sealer(run_id)`).
  When the worker meets a key the keyring cannot open (a missing KEK), it pauses the run with
  `run_paused_key_missing` and hands its leased keys back; a resume clears it once the KEK is back.
- **Purge** (`runner/purge.py`, RETENTION.md §5): the worker runs a pass at most hourly as
  `system:worker`, none within 7 days of 006, and `ds purge` one on demand. A pass cancels or fails
  the runs past the ceiling (`abandon`), then deletes each due cycle in chunks of keys, one
  transaction each, recording the purge before anything irreversible; a blob goes 24 h after its last
  reference. `erase` purges one run or one ended cycle now; `hold` keeps a run until released. A
  pass ends by deleting the table pre-checks made more than `retention.standard_days` ago, 1,000 a
  transaction (`Summary.prechecks`, PRECHECKS.md §8).
- **Approvals** (APPROVALS.md §4): `create_run`, `compare_one` and `resume_run` call `approvals.gate`,
  which in oidc mode refuses a version whose content needs a four-eyes approval it lacks
  (`ApprovalRequired`: 409, CLI exit 2); a soak's boundary attaches the active approval or starts its
  next cycle paused (`run_paused_approval`); tokens and developer modes run, labelled unapproved.
- **Licence** (LICENCE.md §7, §8): `create_run` and `compare_one` call `licence.admit` after the gate,
  before keys are resolved, then again in the insert transaction (a new pair recounted under DSLC); its
  record is `params["licence"]`, and a new pair is audited `licence.pair`. A refusal (`LicenceRefused`:
  409, CLI exit 2; M1-M5) writes nothing (`ds run` and `ds compare` have stored the spec file before, as
  an upload). `resume_run` of a run paused for the licence calls `licence.need`; a soak's boundary
  (`licence.stopped`) starts its next cycle paused (`run_paused_licence`) when the licence refuses runs or
  does not seat its name as of when that cycle falls due (a resume is judged as of then too), and lets it
  go on when the check itself fails. The worker audits `licence.state` when the state changes
  (`licence.watch`, hourly). All of it is a vendor build's: in an open build (§19) `admit` passes every
  name with a null record and no `licence.pair`, `need` and `stopped` refuse and pause nothing (M1 to M7
  are never raised), and `watch` audits the one row `to: "open"`.
- **Notifier** (`runner/notify.py`, ALERTS.md §4, §5, §9): with `notify` set, each sweep of the worker,
  after its cycle ends, starts `notifier.run` as a task when none runs (once without `notify`, to record
  its removal), awaiting nothing, so no notifier fault keeps a cycle from ending. It audits `notify.config`
  when the routing changes, then every `poll_seconds` runs a feed pass (the chain rows after its cursor,
  turned into typed events, matched against the rules and stored as rendered deliveries), a send task per
  due channel (its oldest pending delivery, sent on the notifier's own two threads holding no pooled
  connection, retried with backoff up to `max_attempts`, dead ones audited `notify.dead` at most hourly
  per channel) and, hourly, housekeeping (`notify.daily`, pruning); each step catches everything but its
  own cancellation and logs it. It holds at most two pooled connections (the pool gains two with
  `notify`), none during a send; only `runner/worker.py` imports it, and `close()` cancels it and its
  sends, waiting 10 s at most.
- **Readiness** (`runner/readiness.py`, READINESS.md §4, §8): each sweep of the worker, after its cycles
  end, starts `readiness.watch` as a task when none runs; it never raises, so no readiness fault fails a
  run. A pass measures the cycles ended since 014 (10 a batch) and, at most every 10 s, a batch of those
  ended before it (10 full measures), each batch one transaction ending with its `readiness.measured`
  row; a failed measure (whatever it raised but a lost connection, which ends the batch) is retried
  after 1, 2, 4 and 8 hours, then final. It then evaluates every pair (`deltasleuth/readiness.judged`)
  when a cycle was measured, a row that may change a pair entered the audit chain (at most every 30 s)
  or 15 minutes passed, and chains one `readiness.changed` row per pair whose state, `regressed` or a
  required gate's status differs from its newest row, naming its causes from what differs for the pair
  (`core/readiness.causes`): nothing while a cycle ended before 014 is still to measure, then one
  baseline a pair, and nothing of a pair one of whose cycles was never measured. A process's first
  evaluation audits `readiness.config` when the settings as judged changed, before the rows it writes;
  an evaluation that fails is tried again 30 s later.
- **Schedules** (`runner/schedule.py`, SCHEDULES.md §4, §5): `run_worker(ctx, *, stop=None,
  schedules=True)`; `ds run` passes `schedules=False`, so only the worker of `ds worker` or `ds serve`
  fires, and only while it holds the worker lock. Its first sweep after taking the lock recomputes each
  active schedule's next due time from this build's tz data (`rezone`, a change audited
  `schedule.rezone`). Each sweep reads the due schedules in a savepoint and starts a `fire` task per
  schedule, at most two at once (`FIRING`), never two for one schedule; a due schedule without a slot
  is remembered with its due time and when, so that only a time queued inside its window reads `busy`. `fire` starts only the newest due time, and only inside its window
  (`start_within` minutes, or until the next due time): it is `create_run` as `system:worker`, so the
  gate, the licence and a sample's refusals are the run's own. Every other due time is settled as one
  `schedule.missed` row, or one counted range, with a closed reason and never a refusal's message. A
  live run of the schedule and a failed listing are tried again while the window is open (every 60 s;
  every 300 s, three tries a due time, judged after each attempt); a standing refusal chains its first time and holds the later ones as one
  range, chained within 24 h. A cron or zone the build cannot load pauses the schedule, with a missed
  row only for a time that is due. Whatever
  escapes `fire` is logged and waits 300 s: no firing fault fails a run or ends a sweep, and `close()`
  cancels the tasks, leaving an uncommitted time due.
- **Schema guard** (`runner/drift.py`, PRECHECKS.md §7): it looks at a run's table sides at its creation
  (V1, above), on each scan pass's own sessions before a cursor opens (V2), at a running key-list run's
  cycle start once that cycle fell due (V3, the worker's `self.guards[run_id]`: that cycle's census and
  draw wait for it; a side unreachable is tried once more a lease period later, then the look is
  recorded as not made and the cycle goes on), at `resume_run` of a run it paused (V4: refused, 409, while
  a breaking change stands or a side cannot be looked at; else the run resumes, accepting the changes that
  stand when it was created paused or its spec pauses on schema drift) and after a table read or a hash
  sample's frame refused with a code of the denied class (V5, before `fail_run`). A change that breaks the
  plan pauses the run; any other warns, or pauses under `schemaDrift: {mode: pause}`. A trip is one transaction: the guard judged under the run's
  row lock, the pause, then `paused` and `schema.drift`. It fails no run: a look that cannot be made is
  recorded (`schema.drift {checked: false}`) and the run goes on.
- **Table pre-checks** (`runner/precheck.py`, PRECHECKS.md §5): each sweep reads the due pairs
  (`repo.prechecks_due`) and starts a task per pair (`self.checks`) while each target it names has a free
  pre-check session (`precheck.sessions_per_target`); a pair reads each side in one snapshot bounded by
  `precheck.statement_timeout_s` (catalogue, plan, probe, then the figures), judges it with
  `core/precheck.compare` and settles it in one transaction, which also ends the pre-check and audits
  `precheck.finished` as `system:worker`. A failure is a closed code; an unreachable side is read again
  once a lease period later, a refused credential never; a refused credential settles the target's
  pending pairs of every unfinished pre-check, an unreachable one those of its pre-check. No pre-check fault touches a run or ends a sweep: `_check` never calls `fail_run`.
  Every sweep sets back to `pending` the running pairs no task of this worker reads. `ds precheck run`
  polls beside an in-process `run_worker(schedules=False)`.
- **Census** (PACK.md §1.3): with `packs` or `readiness` set, the worker counts each table side of a key-list run's
  cycle first (`runner/census.record`, one snapshot, an hour a statement) and leases none of its keys
  before; a failure is recorded and the cycle goes on. That census, and a hash sample's draw,
  waits for the schema guard's look at the cycle's start (V3), two tries at most. A table scan counts its snapshots
  (`snapshots_a`, `_b`); a hash sample's draw records its frame as its cycle's census, and no key census
  counts it.
- **Stop conditions:** those in `execution.stop`, plus the API's cancel/pause/resume.
  `maxUnexplainedCritical` counts each key's latest saved result, so a worker restart keeps it; the
  rate windows and `maxConsecutiveErrors` count the current worker process's calls.
- **`compare_one(ctx, spec_id, key, *, who) -> KeyResult`**
  - It is the one-shot path for API, CLI and agent.
  - It creates a `one_shot` run of one key, processes it inline (with the `recheck.oneShot` pass
    when discrepant) and returns the result.
  - A key of a run is compared under that run's spec only (the API's `run_id` form and the agent's
    `compare_key`): another spec would mask it by other rules.
  - The agent's `compare_key` is rate-limited: at most 10 per session per minute.
- **`outcome_changes(ctx, run_id)`:** keys whose outcome changed between soak cycles.

## 5. Reports (from the DB; masked)

- **`write_csv(ctx, run_id, cycle, kind) -> Path | bytes`**, for `discrepancies`, `retry`,
  `coverage`, `normalization`, `root_causes`, `suppressed` and `graph`. The columns are as today,
  with OWASP escaping. A sampled run's `coverage.csv` header appends `SAMPLE_COLUMNS` (`tier target basis
  frame_keys drawn contrast unresolved ub95_one_sided ub95_one_sided_all_suppressed
  ub95_one_sided_worst_case verdict`), and an ended cycle's rows add one per sign-off stratum and a
  `(contrast)` row after `*`, from the stored judgement (SAMPLING.md §11.2); a live cycle's has the
  header without rows. Other runs' header is unchanged.
- **`reports.FORMATS`**: every downloadable report by name (`report.pdf`, `<kind>.csv` and
  `readiness.csv`, the points of the run's pair up to the cycle, at most 500: READINESS.md §11), with its
  media type and renderer. The API route and `ds report --kind` use these names; a new format is
  one entry.
- **`pdf_report(ctx, run_id, cycle=None) -> bytes`** (ReportLab), in this order:
  1. header and spec;
  2. root causes first;
  3. coverage counters, as a compact grid on one page, then a sampled cycle's "Sample and bounds
     (observed)" (`reports/data.strata_section`, from `ds.cycles.sample`, never recounted; the P0 summary
     has it too; SAMPLING.md §14), then an ended cycle's "Labelled accuracy (observed)"
     (`reports/data.accuracy_section`; the P0 summary has it too): what its labelling says
     (`accuracy.lines`, whole: its second line counts the cycle's labellings when there are several) and
     `accuracy.LIMITS`, or `Not measured: no labelled sample of this cycle.` (with how many labellings
     were drawn, none of them labelled); the labelling is the one the
     cycle's requested or approved sign-off attests, else the newest a person labelled, else the newest
     labelled (`accuracy.reported`); a live cycle has no such section (ACCURACY.md §10);
  4. trend against the previous run of the same spec (counter deltas);
  5. "Pair readiness (observed)" (`reports/readiness.run_section`): the pair's parity gates as of the
     reported cycle, judged on the cycles ended by then under the readiness settings it names, without a
     live soak's schedule; or that the cycle is live, not measured yet, or of a run readiness does not
     assess (READINESS.md §11);
  6. outcome changes (soak);
  7. graph parity (a run whose spec has `graph`): the counts, then the first 20 rows of each code;
  8. top findings (masked);
  9. an annex labelled "AI-generated, not evidence" holding the run's chat answers.

  The header states the run's spec approval and the cycle's sign-off (`approvals.label`,
  `signoff_label`: unapproved, revoked, or who approved when; APPROVALS.md §4), as the P0 summary's
  "Four-eyes approval" section and `ds runs` do.
- **P0 tools** (`measure`, `adjudicate`/`score`, `summary` markdown/html) read runs from the DB.
- **Adjudication in the store** (PACK.md §5): `adjudicate.store_draw` and `store_score` keep a cycle's
  reviewer sheets and scores; the P0 summary reads the attested one when its directory has none.
- **Labelled accuracy** (`reports/accuracy.py`, ACCURACY.md): `store_draw` draws a uniform salted sample
  of an ended cycle's items against a named truth source, `store_labels` keeps its sheet once labelled
  (no note, and no hash of the upload) and its score; `tally` counts the labels with exact two-sided 95%
  Clopper-Pearson intervals (`core/sampling.interval`) and, where items are undecidable, the range
  counting them either way, for a label's share and its item counts too; `lines`, `summary`, `LIMITS`
  and `gate` are the only texts built from a score, and every output prints them (`gate`: cut-over
  readiness's accuracy gate over a candidate's labelling, its status and its lines, which name no person
  and are never met for a census that decided nothing; READINESS.md §9 Y18). `listed` is a labelling as lists
  show it (no selection, no score), `detailed` the item. DeltaSleuth counts a person's labels; it decides
  nothing.
- **Readiness reports** (`reports/readiness.py`, READINESS.md §11): `dashboard_document` is the
  dashboard of the pairs a selection takes, worst first, 100 pairs a volume (the definition, the
  settings hash, each pair's gates with their texts, convergence, sign-off, pack and last evidence
  cycles, and what the report is not); `dashboard_csv` one row per pair and gate; `trends_csv` one row
  per pair and point, counted first and refused over 100,000 rows on download. All are built from the
  evaluation every surface shares; none gives a percentage of readiness, a forecast or a key.
- **Evidence packs** (`reports/pack.py`, PACK.md): `problems` gives every reason a signed-off cycle gets
  no pack, the licence's first (in a vendor build an Enterprise licence at issue; in every build never
  a run made in evaluation:
  LICENCE.md §6); `issue` builds, signs (ECDSA P-256) and stores one, its manifest and certificate naming
  the licence (an open build's name none: `licence` null, no certificate line, §19); `revocations` signs the revocation list; `pack_verify.verify` checks a ZIP offline as its
  `VERIFY.txt` does.
- **Licence labels** (LICENCE.md §10): `report.pdf`'s header adds the licence the run was made under
  (`reports/data.licence_line`), the P0 summary's warning an evaluation or grace label, and an
  evaluation run's CSV and PDF files are named `R-…-unlicensed-evaluation-<name>` (`licence.file_name`),
  their content unchanged. A run an open build made records no licence, so it has no label, line or
  warning and its files are `R-…-<name>` (§19).
- **Table pre-checks** (`reports/precheck.py`, PRECHECKS.md §11): `precheck.csv` (a row a pair, `claim`
  `observed; not parity`), `precheck-columns.csv` (a row a pair, column and figure; withheld cells empty)
  and `precheck.pdf` (`Table pre-check (observed)`), each headed by `precheck.NOT` (a CSV's first record,
  the evaluation label its second) and every cell through `escape_cell`; downloaded from `GET
  /v1/prechecks/{id}/reports/{name}` or `ds precheck report`, audited `report.download` before the bytes,
  named `PC-…-unlicensed-evaluation-<name>` in an evaluation. They are no run report and no pack holds
  them. `report.pdf`'s title block gains one sentence when the run has `schema.drift` audit rows of its
  cycle or an earlier one (`drift.sentence`: the changes that paused it and the others, and the cycle
  starts and other looks it could not check); no CSV of a run changes.
- **Claim level:** every MVP run is `observed` (`certified` needs the deferred frozen time);
  `coverage.csv` records it, and the PDF and API carry the level once `certified` exists.

## 6. Agent

- **Tools** (JSON-schema, masked outputs, capped). Every tool is bound to the chat session's run:
  none takes a run or a spec, so a chat about one run can only read and cite that run.
  - `get_run_summary()` returns counters, status, the top 10 root causes and, for graph parity, the
    scan's `graph` counts; for a sampled run, `sample`, the stored judgement of the current cycle once
    ended, else of the one before (SAMPLING.md §14); and, for every run, `accuracy`, that same cycle's
    labelled accuracy as stored, or null until one of its labellings is labelled: `{accuracy, summary,
    attested}` of the labelling the reports state (ACCURACY.md §10). `accuracy` is the stored score cut
    to its first 10 root causes, in rank order, and the 10 rules with most labelled keys (then most keys,
    then by rule id), `by_cause_more` and `by_rule_more` counting the ones left out; `summary` is
    `accuracy.summary(…, named=False)`'s line, which says what kind of actor labelled (`by a person`,
    `through a token`, `in developer mode`) and never a name or an id; `attested` is the id of the
    sign-off attesting it. No actor's name or id reaches the model. The model explains stored numbers; it
    labels and computes nothing.
  - `get_findings(key?, root_cause_id?, taxonomy_code?, path_pattern?, severity?, limit<=50)`
    returns findings with `F-` ids.
  - `get_observations(key)` returns the masked canonical A and B (≤ 4 KiB each), the first 20
    findings (`more_findings` flags the rest), the `O-` ids, and `cycle`, the cycle of the key's row
    it read. A key the run does not hold is the error `no key <key> in run <id>`, from `compare_key` too.
  - `compare_key(key)` returns a one-shot result for a key of the run, under the run's own spec; a
    four-eyes or licence refusal (LICENCE.md §8) is an error result the model reads.
  - Every text cell of a tool result is cut at 512 characters.
  - The tools follow the asker's roles: `compare_key` needs operator, the others viewer. The model is
    offered only `tools_for(who)`, and `run_tool` refuses the rest. The agent's compares are audited
    as the asker, with `via: chat:S-…`.
- **The MCP server** ([MCP.md](MCP.md)) serves these four tools to an external agent, each with a
  required `run_id`, through the same `run_tool`, beside seven reads of its own. `agent/prompt.py` holds
  the system prompt as `INTRO`, `UNTRUSTED`, `MASKED` and `CITE`, joined as `SYSTEM` (the text as it
  was: a test holds its hash); the server's instructions say the last three. `core/jsonio.plain`
  renders a row's times and addresses as text for the API's answers (`deps.ok`) and for an MCP result.
  None of what follows applies to an external agent: DeltaSleuth runs no loop for it and validates no
  answer it writes.
- **Loop:**
  - system prompt, then the user question with scope, then up to 8 tool calls, then the answer.
  - Tool results are data (injection-safe framing). A tool's refusal (bad arguments, an unknown
    id, a runner ValueError) is an error result the model can read, not a failed turn.
  - The profile's budgets are enforced: parallel calls past `max_tool_calls` get an error result
    and the next request forces the answer. The profile's `limits` are shared by every chat
    question (`ProfileRegistry.gate`).
  - A reply that stops on `max_tokens`, `content_filter` or `refusal` abstains.
- **Answer contract:** text with inline citations `[F-…]`, `[O-…]`, `[RC-…]`, `[K:<masked key>]`.
- **Validator:**
  - The answer cites at least one id, and every cited id must appear in this session's tool
    results.
  - Every number in the answer (`1,234` is one number) must appear in a tool result. A count in
    shorthand (`2.4M`, `37k`, `3bn`, `1e6`, `1.250.000`, `900_000`: a magnitude, an exponent, or groups
    by dots or underscores) is refused as written, unless a tool result's text shows that very text.
  - On failure it asks for one repair, then abstains with a clear message. The repair request quotes an
    unknown citation back to the model; the abstention, which is stored and shown, names it by its kind
    (`a [K:...] citation`) unless it has a stored id's shape (`F-` or `O-` with 16 hex digits, `RC-` with
    12): no text the model wrote inside a bracket is stored or shown.
- **Fixed questions** (`POST /v1/chat/sessions/explain`): `prompt.EXPLAIN`, about one root cause of the
  run's current cycle, and `prompt.SUMMARISE`, about the run. The product writes them, neither holds a
  digit, and each is asked in a run-scoped session of its own and answered as an AI draft.
  - The agent acts as a viewer whoever asks: `compare_key` is never offered and no target is called.
  - `answer(…, about=)` appends a blank line and the root cause as `get_run_summary` sends one
    (`tools.cause_facts`), framed like a tool result, to `EXPLAIN`, sends and stores both, and takes the
    facts as evidence of the turn. `check(…, must=)` then asks that the answer cite that root cause.
  - A stored assistant message with `citations: []` is a refusal (the abstention after one repair, or a
    failed call), any other a verified answer. A draft is a chat answer: `report.pdf`'s annex prints it.
  - Limits: the validator checks ids and numbers, never a word or a meaning. A wrong count passes when
    the same number stands anywhere in the session's tool results, as do Markdown and bullets; a number
    in words (`two million`) or with another abbreviation (`5MM`, `10x`) is not read; a session
    continued through `POST …/messages` loses the root cause's facts as evidence.
- **Persistence:** it stores messages, tool calls, citations and `llm_calls` rows, in one
  transaction, even when the turn fails. Tool results are masked; the question is stored and sent
  as typed (nothing masks free text).
- **LLM port** (`deltasleuth/llm`): one request shape (messages, tools, `tool_choice` auto or none,
  an output cap, data classes) over the `openai_chat` and `anthropic_messages` dialects.
  `ds llm-probe` checks a profile at onboarding (a baseline request, a tool round trip, cached-token
  usage); it never changes what the running app uses.
- **AI off** (no profiles file): chat endpoints return 503 "AI disabled"; everything else works.
  An unreadable key file of the chat profile is also a 503, naming the file.

## 7. API v1 (FastAPI; JSON; roles)

Every route but `/v1/health` and the three sign-in routes (`login`, `callback`, `token-login`) declares
its caller and the role it needs ([AUTH.md](AUTH.md) §4.1); `create_app` refuses a route that does
not. A caller is an API token (`Authorization: Bearer dst_…`), a cookie session (SSO, or token
sign-in in tokens mode; writes need `X-CSRF-Token`), or in developer mode `dev:loopback`. `ds serve`
refuses to listen beyond loopback without the `auth` settings, and warns on loopback without them.

Every response carries a CSP (`default-src 'self'`, `frame-ancestors 'none'`, `form-action 'self'`),
`nosniff`, `no-referrer` and `X-Request-ID`; `/v1` responses add `Cache-Control: no-store`, and an
https `public_url` adds HSTS. A `/v1` request's `Host` must be the `public_url` host, an
`auth.allowed_hosts` entry or a loopback name (else 400). A write from another origin or site is
refused (403), and so is a read another site's page makes (a link or embed of a `/v1` URL), except
health and the sign-in routes. A body over 10 MiB is refused (413) before it is parsed. No `/docs`, `/redoc` or
`/openapi.json` is served. New work a vendor build's licence refuses answers 409 with its message
([LICENCE.md](LICENCE.md) §8, §9; an open build refuses none, §19); no route installs or withdraws a licence. Below `POST /v1/mcp` the MCP
library answers in its own bodies, some of them plain text: the one exception to JSON errors here
([MCP.md](MCP.md) §11). A 404 under `/.well-known/` is JSON too, not the UI's page. An MCP tool's role
refusal is audited through the throttle the routes' refusals share (`app.state.denials`), and
`Throttle.forget` (`auth/principal.py`) drops a key whose audit row could not be written, so that the
next hit is due one: an `mcp.read` row that failed to commit is written at the run's next read.

| Route | Returns |
|---|---|
| `GET /v1/health` | status, AI on or off, and the auth mode; 503 `the database is not answering, or the pool gave no connection in 3 s` when the pool gives no connection in that time, or at once when the connection it gives fails; the statement itself is not bounded ([FIRSTRUN.md](FIRSTRUN.md) §8, S2) |
| `GET /v1/auth/login`, `GET /v1/auth/callback` | OIDC sign-in: 303s to the IdP, then to the UI |
| `POST /v1/auth/token-login` | tokens mode: a cookie session for an API token |
| `GET /v1/auth/me`, `POST /v1/auth/logout` | who is calling, with the CSRF token; sign-out |
| `GET`, `POST /v1/auth/tokens`, `DELETE /v1/auth/tokens/{id}` | API tokens (admin; the token is shown once) |
| `GET /v1/auth/sessions`, `DELETE /v1/auth/sessions/{id}` | sessions (admin) |
| `GET /v1/auth/users`, `POST /v1/auth/users/{id}/disable`, `/enable` | people and their roles at the last sign-in (admin) |
| `GET /v1/audit?actor=&action=&object=&session_id=&since=&until=&cursor=` | the audit log, paged (admin) |
| `GET /v1/targets` | `{items, declarer}`: ids, declared capabilities, a database target's `engine` (`postgresql`, `mysql`, `oracle` or `files`; null for a service) and `declared_in` (`file`: the targets file; `product`: the product's list), with the role that declares (`admin`; in a trial `spec_author`): no secret, host, user or files root. To a holder of that role a target of the product's list also shows what it was declared with: `base_url`, `allow_private_network`, `allow_insecure_http`, `auth: {kind, header, more: [header]}`, `has_secret`, `rate_per_s`, `max_concurrency` and `absent_on_404` |
| `PUT /v1/targets/{id}`; `DELETE /v1/targets/{id}` | a service declared in the product's list, or changed, without a restart: `{base_url, allow_private_network?, allow_insecure_http?, auth?: {kind, header?, more?: [{header}]}, secret?, more_secrets?: {header: value}, rate_per_s?, max_concurrency?, absent_on_404?}` in, judged as an entry of the targets file; 201 or 200 with its item. A credential not sent is kept while the base URL, the kind and its header stay. The delete removes the entry and its credentials (204). 409 for an id the targets file declares, and for a target unfinished runs read (a removal, or a change of more than its credentials and budget); 404; 422 `invalid target` with the targets file's issues (and `container_localhost`), or for a credential that is required, refused (one that holds this installation's own session or sign-in cookie too) or not taken; 500 `the target could not be written: …` (who declares: [AUTH.md](AUTH.md) §4.1; DEPLOY section 7) |
| `GET /v1/licence` | the installation id, the licence's state and warnings, the licence and its renewal, what it `allows`, the pairs counted with their seats, and each stored licence (viewer, unaudited; `state: unknown` when it cannot be read; in an open build `state: open`, no warning, `licence` null, every `allows` true and no pair counted, LICENCE.md §19) |
| `POST /v1/specs` (YAML, JSON or the builder's form, below) | validated and stored spec, with the scopes that gate its runs (`needs`) and its active `approval_id` (422 with pointers on error) |
| `GET /v1/specs`, `GET /v1/specs/{id}` | specs; the list adds each version's newest `approval` request; one spec adds `created_by_name` (null where no actor was recorded) and `yaml`: its body, inline keys masked by `key.class`, rendered as YAML under two comment lines, `<name> <version> (spec <id>), content hash <hash>` and `As stored, parsed: the upload's comments and key order are not kept; inline keys are masked.`, which ends `inline keys are in clear (key.class business_code).` for that class, the default (the upload's own text is not kept) |
| `POST /v1/specs/{id}/approvals`, `POST /v1/runs/{id}/cycles/{cycle}/signoff` | a spec version's four-eyes approval or a cycle's sign-off requested (spec_author, operator; SSO only) |
| `GET /v1/approvals?status=&kind=&spec_id=&run_id=&cursor=`, `GET /v1/approvals/{id}` | approvals, paged; one adds `attests`, its `review` and whether the caller can approve it |
| `POST /v1/approvals/{id}/decision`, `POST /v1/approvals/{id}/revoke` | approve (a sign-in within 15 minutes) or reject; revoke (rule_approver, a person) |
| `POST`, `GET /v1/runs/{id}/cycles/{cycle}/adjudications`; `GET /v1/adjudications/{id}/sheet.csv`; `POST /v1/adjudications/{id}/score` | draw a reviewer sheet (operator), list, download (viewer), score its upload (operator, `text/csv`); SSO only (PACK.md §7.1) |
| `POST`, `GET /v1/runs/{id}/cycles/{cycle}/labellings`; `GET /v1/labellings/{id}`, `/sheet.csv`; `POST /v1/labellings/{id}/labels` | draw a labelling of an ended cycle against a named truth source (operator; 201 with its items and warnings, 200 with the first labelling for a draw repeated within ten minutes, 504 when the draw was stopped), list (newest first; a list item holds its ids, actors, dates, sizes, `item_count`, truth, salt and hashes, `summary` and `attested`, the sign-off attesting it: no sheet, no selection, no score), read one (the item: also its selection's counts, its score, `cycle_drawn` and `cycle_labelled`, its `lines` and `limits`), download its sheet (viewer), label it once with the filled sheet (operator, `text/csv`; the item as labelled); `{cycle}` is 1 to 2,147,483,647, else 422; every auth mode (ACCURACY.md §10) |
| `POST /v1/runs/{id}/cycles/{cycle}/packs`; `GET /v1/packs?run_id=&status=&cursor=`, `GET /v1/packs/{id}`, `/download`, `GET /v1/packs/revocations` | issue an evidence pack (an operator person); list, read with its manifest, download (audited first) and the signed revocation list (viewer); SSO and `packs` only |
| `GET /v1/samples?spec_name=&cursor=`, `GET /v1/samples/{draw_id}`; `POST /v1/samples/close` | a name's ledger, or every draw paged; one draw with its ledger and its live `progress` or its sign-off `precheck` (its nonce withheld while live, and with its strata's `u_max` below rule_approver) (viewer); close a name's ended `bottom_k` draws that did not pass, with a note of the fix (an operator person, SSO only; SAMPLING.md §14) |
| `GET /v1/notifications`, `GET /v1/notifications/deliveries?channel=&state=&event=&run_id=&cursor=` | whether notifications are on, the feed's cursor, each channel with its counts and newest attempt, the rules, the active mutes and the banners; the deliveries newest first with their stored bodies (viewer; never a URL, path, address or secret) |
| `POST /v1/notifications/channels/{channel}/test`, `/retry` | one test attempt now, stored as sent or dead (one per channel in 10 s, else 429); the channel's dead deliveries since `since` (72 h) pending again (operator) |
| `POST /v1/notifications/mutes`, `POST /v1/notifications/mutes/{id}/end` | a mute of a channel or run for 1 to 168 hours, `201 {mute_id, until}` (an admin signed in within 15 minutes); its end (admin). Every notification POST answers 409 while `notify` is absent (ALERTS.md §10, §11) |
| `GET /v1/readiness?programme=&entity=&state=&gate=&closed=&q=&as_of=&limit=&cursor=`, `GET /v1/readiness/pairs/{name}?precheck=1`, `/cycles?bucket=&cursor=`, `/brief`, `GET /v1/readiness/transitions?pair=&programme=&limit=&cursor=` | every pair's gates and place on the ladder, worst first, with each programme's and entity type's roll-up (`as_of`: what the worker had recorded by then); one pair's gates, convergence, points, versions, live soaks and strata, with the sign-off precheck on demand only; its trend by cycle, day or week; its cut-over brief (its gates, the stored figures of its newest complete cycle, its last 30 measured cycles with their marks and its root-cause groups as opened, still open or closed; computed on read, no score and no forecast); the recorded `readiness.changed` rows, newest first (viewer, unaudited; never 410; READINESS.md §10) |
| `GET /v1/readiness/reports/{name}?programme=&entity=&pair=&bucket=&volume=` | `dashboard.pdf` (100 pairs a volume), `dashboard.csv` or `trends.csv` (refused over 100,000 rows), or with `pair` its `brief.pdf` (one page), `brief.csv` or `causes.csv` (refused over 100,000 rows; each of the three 422 without `pair`), downloaded as `readiness-<name>` and audited `report.download` on object `readiness` (viewer; READINESS.md §11) |
| `GET /v1/schedules?spec_name=&state=&set_by=&limit=&cursor=`, `GET /v1/schedules/zones`, `GET /v1/schedules/{id}`, `GET /v1/schedules/{id}/firings?limit=&cursor=` | what a schedule never tells, this build's tz data, how many schedules are overdue, the banners and a page of schedules (the deleted only with `state=deleted`); the zones a schedule takes and each renamed zone's primary; one schedule (by id or live name) with the gate's state for what it would run, its next five times and its newest 20 firings; its firings paged, each with its text (viewer; SCHEDULES.md §10) |
| `POST /v1/schedules/preview`; `POST /v1/schedules`, `PUT /v1/schedules/{id}`, `POST /v1/schedules/{id}/pause`, `/resume`, `DELETE /v1/schedules/{id}` | a cron's next ten times in a zone and its notes (a zone whose name holds a digit, each clock change), writing nothing; `201` the schedule with `warnings`; the definition replaced as loaded at `revision` (409 if it changed since); paused; resumed; `{schedule_id, deleted_at}` (operator; a refusal is SCHEDULES.md §9's message with its status) |
| `POST /v1/specs/validate` | issues; a form (below) whose text parses is also answered as `form`, split again |
| `POST /v1/specs/try` | `{form, side, key}` in: one side's request sent once for one key, as a run reads the side (the target's gate, the guard, the spec's timeouts; 1 MiB at most, its pages together), storing nothing. The body is read as a spec document is: a number no float holds (`number_out_of_range`), a nesting over 64 levels or a repeated member is 422 `invalid spec`, before anything is audited or asked. `{side, target, method, status, error_class, error_detail, attempts, pages, ms, bytes, content_type, answer, note, raw}` out, never a URL: `answer` is the parsed JSON as it came in a trial (`raw: true`), elsewhere its structure with every string and number a stand-in and a member name that reads as data too (`raw: false`): an id by the profiler's rule, or a name holding three digits in a row or four in all (an account or card number, however prefixed or dashed); a name that is data with fewer digits (`Smith`, `acct_abcdef`) shows. In both, a string of the answer (a member name too) that is a credential stored for the target, or holds one of eight characters or more, reads `[the target's credential]`. `answer` is null without a body, for a body over 1 MiB (`resource_limit`), of a 401 or 403 (`auth_error`), and, with why in `note`, for one that is not JSON, that nests deeper than 256 or holds a number beyond 1e4000 or, in a trial, whose numbers written in full take over 4 MiB of text (a 1 MiB body may hold thousands of numbers such as `1e3999`). A form is `{text, sides: {a: {target, request?}, b: {…}}}`, a JSON body `POST /v1/specs` and `/validate` take too: the text's document with each sent side's `target` and `request` in place of the text's; `GET /v1/specs/{id}?form=1` answers a stored spec as one (`form`: its text is YAML, or JSON where the YAML would not read back as the document, a character the YAML reader does not take raw (DEL, C1, U+2028, U+2029, U+FFFE, U+FFFF) written as its escape; left out where neither reads back). Where the other side of a tried form has no request yet, the side tried stands in for it, and an issue of what stands in is said once, under the side tried. 422: a side sent for a side holding `table`; inline keys that are stand-ins, as a stored spec's are shown; a side tried without a target and a path |
| `POST /v1/profile` | `{pairs: [{key, a, b}]}` in, draft spec YAML, evidence, notes, each side's `unmatched` paths (`{a: [...], b: [...]}`) and the draft's issues out |
| `POST /v1/compare` | `{spec_id, key}` in, or `{spec_id, run_id, key}` with a masked key of that run and its spec; the one-shot result's key view out, with `warnings` (a table pair's changes since the version's last baseline run, or while none is one its newest stored plan: PRECHECKS.md §7). 422 with the guard's or the connector's own text for a target the address guard refuses or a template the connector does (as `POST /v1/specs/try`) |
| `POST /v1/runs` | `{spec_id, mode, keys?, soak?}` in, `202 {run_id, warnings}` out (`warnings`: the schema guard's lines of the tables' changes since the version's last baseline run (while none is one, its newest stored plan), the pause's line first for a run created paused; a hash sample's refusals R1–R4: 409 or 422; E6, a narrowing under `schemaDrift: {mode: pause}`: 422, PRECHECKS.md §10) |
| `GET /v1/runs`, `GET /v1/runs/{id}` | runs, each with `licence` (its record and `label`, or null); one run adds its progress, `retention`: `{class, days, hold, purged, last_purged_at, cycles: [{cycle, ended_at, expires_at}]}`, its spec `approval` (`state`, `approval_id`, `needs`, `mode`, `deciders`, the revoke), its cycles' `signoffs`, `census_pending`, `cycles` (each ended cycle's `census`, `adjudications`, `labellings` (list items, as the list gives them: no selection, no score), `packs` and `sample`: its draw, without nonce and strata `u_max` below rule_approver, and judgement), a sampled run's `sample` (`{state, error, draw_id, drawn_at}`) and `schedule` (`{schedule_id, name, due, time_zone, late_s, text}` for a run a schedule started, else null), and `schema` (`{guarded, mode, basis, checked, paused, accepts, lines}`: what the schema guard holds of its tables, as the server's lines; PRECHECKS.md §11). `GET /v1/runs` takes `schedule_id=`: the runs that schedule started. No run row shows `guard` A run with a file side answers its plan's counts and times but never `params.pinned` (the sealed pins) or a side's `files_sha256` (FILES.md §16) |
| `POST /v1/runs/{id}/{cancel,pause,resume}` | new status; a resume of a run the schema guard paused is refused (409, U1 and U2) while a change that breaks its plan stands or a side cannot be looked at, and otherwise accepts the changes that stand when the run was created paused or its spec pauses on schema drift (PRECHECKS.md §7, V4) |
| `POST /v1/prechecks`; `POST /v1/prechecks/catalogue` | a table pre-check of stored specs (`spec_ids`, `spec_names` or `all_specs`, and `level`; operator), or of two declared database targets' schemas paired by name (`a`, `b`, `include`, `exclude`, `level`; spec_author): `202 {not, precheck_id, pairs, skipped, evaluation}`, its pairs read by the worker (PRECHECKS.md §3, §11; K1 to K10) |
| `GET /v1/prechecks?limit=&cursor=`, `GET /v1/prechecks/{id}?verdict=&limit=&cursor=`, `GET /v1/prechecks/{id}/pairs/{n}` | `{not, banners, items, next_cursor}`, each pre-check's progress and counts by verdict; one pre-check with its totals, `banners`, `worded` and its pairs worst first (`{n, label, spec_id, status, verdict, differing, rows, text}`), selecting no `result`; one pair's `result` with its lines and each compared column's (viewer) |
| `POST /v1/prechecks/{id}/cancel` | `{not, precheck_id, cancelled_at}` (operator; 409 once it has ended) |
| `GET /v1/prechecks/{id}/reports/{name}` | `precheck.csv`, `precheck-columns.csv` or `precheck.pdf`, audited `report.download` before the bytes (viewer) |
| `GET /v1/prechecks/{id}/pairs/{n}/draft.yaml` | a starter spec of two relations paired by name, its issues as header comments; never stored, unaudited (spec_author; K12) |
| `GET /v1/runs/{id}/results?outcome=&cursor=` | paged results |
| `GET /v1/runs/{id}/findings?…&cursor=` | paged findings |
| `GET /v1/runs/{id}/root-causes` | root causes |
| `GET /v1/runs/{id}/keys/{key}` | result, findings, masked canonical A/B |
| `GET /v1/runs/{id}/changes` | outcome changes across soak cycles |
| `GET /v1/runs/{id}/graph?cycle=&cursor=` | graph parity rows (`ds.graph_diffs`), paged |
| `GET /v1/runs/{id}/reports/{name}` | a `reports.FORMATS` name: `report.pdf`, `<kind>.csv` or `readiness.csv`, downloaded as `R-…-<name>` (`R-…-unlicensed-evaluation-<name>` for a run made in evaluation) |
| `POST /v1/chat/sessions`, `POST /v1/chat/sessions/{id}/messages`, `GET /v1/chat/sessions/{id}` | chat |
| `POST /v1/chat/sessions/explain` | `{run_id, root_cause_id?}` in: a run-scoped session of its own, asked a fixed question (§6) about that root cause of the run's current cycle or, without one, about the run; `201` the session as `GET /v1/chat/sessions/{id}` gives it, its last message the draft or, with `citations: []`, the refusal; 404 `root cause RC-… not found` for one the current cycle lacks, 422 for a malformed id, and nothing is created; 503 for an unreadable key file or a missing KEK, the empty session staying (viewer) |
| `GET /v1/llm/profiles` | ids, dialect, model, trust (no secrets) |
| `POST /v1/mcp`; `GET`, `DELETE /v1/mcp` | one JSON-RPC message of an agent's MCP client, answered as JSON by the MCP server (`tools/list`, `tools/call`): stateless, no stream and no `Mcp-Session-Id`, a body of 65,536 bytes at most, a framed result of 65,536 bytes at most (viewer, by an API token or in developer mode, never a cookie session: 403; 404 unless `mcp.http`); 405 with `Allow: POST` (MCP.md §5.1, §11) |

**Expired and unreadable evidence** (RETENTION.md §4.4). Once a cycle's purge has started, its routes
(results, findings, root causes, graph, keys, reports but `readiness.csv`) and `POST /v1/compare` with its run answer 410
`cycle N of run R-… was purged on <day> (<reason>): its keys, results, findings and evidence are
deleted`, as do a labelling's draw, `sheet.csv` and upload (its list and item still show the tombstone
with its score), and chat answers 410 once the run's current cycle is purged. The readiness routes never
answer 410: a purged cycle keeps its measures. A missing blob answers 410
`evidence of O-… is missing (purged or lost)`. A key the keyring cannot open answers 503
`evidence unreadable: …`. No route purges, holds or manages keys: that is the CLI's (host
administrators).

## 8. UI (web/)

Vite + React 19 + TypeScript, no UI framework, a small hand-written API client and minimal CSS.

| Screen | What it does |
|---|---|
| Runs | Lists runs and creates a run: pick a spec, choose batch or soak, paste keys. |
| Run | Counters and progress (polling), under the outcome badges `What the outcomes mean` (a line an outcome, `LEGEND` in `Run.tsx`, which a test holds to `core.types.Outcome`), its spec's name linking to the stored spec, what its spec approval says, `Pair readiness: not ready (6 of 9 gates)` linking to its pair and `cut-over brief` to the pair's brief (a batch or soak run's), the schedule that started it (the server's `schedule.text`, linking to the schedule), the schema guard's lines (the server's `schema.lines`, the first a banner while the guard holds the run paused, and `Resume (accept the changes)` where a resume would accept them: PRECHECKS.md §11), root causes, findings table with filters, report downloads (saved as `R-…-<name>`, an evaluation run's as `R-…-unlicensed-evaluation-<name>`), soak cycle selector and outcome changes; until when the cycle's evidence is kept, a banner instead of the tables for a purged cycle, each ended cycle's sign-off (requested there under SSO), and its census, adjudications (Draw, Download sheet, Upload reviewed sheet) and evidence packs (Issue evidence pack, Download); each ended cycle's "Labelled accuracy" block, in every auth mode (Draw with its sizes, truth source and date, the drawn items linked to the Key screen, Download sheet, Upload labels, then the server's `lines` and `limits`; ACCURACY.md §10); a sampled run's Sample panel (frame, strata with progress, interim bound, ETA, the precheck, the ledger, and "Record the fix and close failed draws"). |
| Key | Side-by-side masked canonical A/B JSON with the differing pointers highlighted, the findings, and a "compare now" button. |
| Specs | Lists specs with their approval and their name's schedules (each with its next due time; `Schedule…` opens the schedule form with the version, and `Pre-check…` the table pre-check form, for operators), uploads and validates YAML (issues with pointers), drafts a spec from uploaded sample pairs (a side whose sample status is not 2xx has no document), showing the draft's notes, each side's unmatched paths (`unmatched[a]: …`, `unmatched[b]: …`, or `none`) and its evidence under the form, and under SSO requests a version's approval. `#/specs/<spec_id>` (`SpecScreen`; reached from a name in the list, the Run screen's spec name and the readiness screens' versions) shows one stored version read-only: its name, version, `Spec #12 · content hash <64 hex> · stored <when> by <name>` (`by an unrecorded actor` where none was recorded), `All specs` and the API's `yaml` in a `<pre>`; for a spec author it opens in the comparison builder instead, and `New comparison` on the list opens the builder at `#/specs/new`. |
| Comparison builder (`Builder.tsx`, `building.ts`) | For spec authors, at `#/specs/new` (on `building.STARTER`, named `scratch-comparison`) and at a stored spec's `#/specs/<spec_id>` (`GET /v1/specs/{id}?form=1`). `Sample key`, in memory only; `Side A` and `Side B`, each a service side's `Target` (service targets), `Method`, `Path`, `Query` and `Headers` (a line a member) and, for a POST, `Body (JSON)`, or `A table side: edit it in the text.`; `Import curl…` (`fromCurl`, in the browser: one bash `curl` command into the rows, less the target's base path, without a browser's conditional headers and DeltaSleuth's own session and sign-in cookies, a credential header never among them; it mirrors the loader's credential rules by the fixture `web/test/credential-headers.json`, which both test suites read) and `Send` (`POST /v1/specs/try`), which shows `HTTP 200, 143 ms, 1 attempt, 12,345 bytes`, then the answer under `The answer as it came: …` in a trial or under `Every value is a stand-in: equal values get equal stand-ins.`, or the note, or a 422's text and issues. `Everything else (YAML)` holds the rest of the spec; a side is sent only when its rows name a target, and a stored request goes back untouched until a row is edited. `Validate` (text pasted whole fills the rows it left empty), `Save as a new version` and `Save and compare this key`, which shows the Key screen's detail under `Compared now (run R-…)`. A command's credential goes to the Targets form through `Declare the endpoint…`, as `Change <id>` of the side's target where that one is of the product's list and the command reached it (`building.reached`: the same scheme and port, and the same host or two names of this machine), else as a new endpoint at the command's origin and the base path taken off; unsaved work survives that visit (`Your unsaved work on this page is back.`) |
| Targets (`Targets.tsx`) | For every role, at `#/targets`, in the nav after Specs: each target's `Id`, `Declared in` (`targets file`, `product`) and `Kind`. Who declares (`GET /v1/targets`'s `declarer`) also sees what a target of the product's list was declared with, `Change` and `Remove` (confirmed: `Stored specs and schedules that name <id> stop running.`), and the form `Declare an endpoint`: `Id`, `Base URL`, `Private or local network`, `Credential over plain http`, `A 404 means no such record`, `Authentication` (`None`, `Bearer token`, `Header` with `Header name`), `Credential` (a password box in no form, `autoComplete="off"`; `stored: leave empty to keep it`), `Add another credential header` (up to three), `Requests a second`, `At once`, `Save endpoint`. A save empties the form; a refusal keeps it and lists the issues; `reauth_required` offers Admin's sign-in again. While the form holds a credential a command carried, `Change` on a row the command reached lays it over that target, and on any other row asks before the credential is dropped. Others read `Endpoints are declared by the <declarer> role, or in the targets file.` |
| Schedules (`Schedules.tsx`) | For every role, in the nav after Specs. `#/schedules`: what a schedule never tells, the server's banners, and each schedule's name, spec and version (`pinned 1.4.0 (spec 12)` or `follows customer-profile: 1.4.0 (spec 12) now`), mode, cron and zone, next due time in its zone and in UTC (`a schedule, not a forecast`), newest firing in the server's words with its run's status, and state; operators get the form (spec, follow switch, mode, a preset or custom cron, zone, start within, end, Preview). `#/schedules/SC-…`: its definition, revision, who saved it, the gate's state, its next five times and its firings, paged; operators Edit, Pause, Resume, Delete and Move to the newer version (SCHEDULES.md §10). |
| Pre-checks (`Prechecks.tsx`) | For every role, in the nav after Schedules (PRECHECKS.md §11). `#/prechecks`: what a table pre-check is not (`NOT`), the server's banners, and each pre-check's level, source, who and when, progress and counts by verdict; operators get the form of stored versions (or every stored spec of two tables) and a level, spec authors the form of two database targets' schemas paired by name. `#/prechecks/PC-…`: progress, the totals, the pairs worst first with a verdict filter, the three downloads and Cancel (operators). `#/prechecks/PC-…/pairs/N`: the pair's lines, refusals, schema table and each compared column's lines, `Run a comparison` (operators, a stored spec's pair) and `Draft a spec` (spec authors, two relations paired by name once both catalogues were read). Verdicts are words, never a tick or green; no screen builds a text. |
| Approvals (`Approvals.tsx`) | Under SSO: one list with a status filter and a "Two approvers needed" marker; a review with the earlier requests above Approve and Reject (disabled with the reason), then a spec version's needed items, exclusion set and diff against the version approved before, or a sign-off's checklist, attested census and adjudication, a format-2 sign-off's draw, frame, strata and ledger, a format-3 sign-off's attested labelling (the server's `lines` and `limits`) and the cycle's other labellings' `summary`, counters, rules and root causes; Revoke for an approved one. |
| Chat | A drawer on the Run and Key screens, scoped to them. Citation chips link to findings and keys; a chip is a citation as the validator reads one (`CITATION` in `explain.ts`, the server's expression, which a test holds to it). Its input is disabled once the run's current cycle is purged. |
| AI drafts (`explain.ts`) | On the Run screen's "Root causes": `Explain` on each row and `Summarise this run` ask a fixed question (§6) in one request and show the cited draft under the table as `AI draft: root cause RC-…` or `AI draft: run summary`, beside `Engine facts (the comparison decided these, not the AI)`: the root cause's row, or the run's status, stored `counters` (not `progress`) and root-cause count, a scan's line and, above 10 root causes, `This draft read the first 10 of N root causes.` A stored answer that cites nothing shows as `No AI draft` with its message as plain text. In a draft `[K:…]`, `[F-…]` and `[O-…]` link to the Key screen of the draft's cycle, the last two with `cite=<id>`, whose row that screen outlines. One draft is open per run and cycle (`sessionStorage` `ds.explain:<run>:<cycle>`; `Ask again`, `Close`). The buttons are disabled, with the reason as their title and a line under the heading, while AI is off, for an older cycle and without a root cause; a failed request shows its `detail`. |
| Sign in | Single sign-on (oidc) or an API token field (tokens mode); a sign-in error shows a fixed message and the request id to quote. Under the token field stand where a token comes from (`TOKEN_HINT`) and, for a sign-in refused as `cross-origin request refused`, the address the page was opened at and what to try (`addressHint`, from the address bar alone); a token sign-in whose session cookie the browser did not send back says so (`NOT_KEPT`). The three texts are `web/src/signin.ts`'s ([FIRSTRUN.md](FIRSTRUN.md) §6, [AUTH.md](AUTH.md) §9). The nav shows the name with its id, and Sign out. |
| Admin (`Admin.tsx`) | For admins: API tokens (shown once; creating one needs a recent sign-in), users (disable, enable), sessions and the audit log (with each row's chain `seq`), one tab each. |
| Notifications (`Notifications.tsx`) | For every role, at `#/notifications`, in the nav while notifications are on: the feed's cursor, each channel with its counts (Slack and Teams badged `external`; Send test and Send failed again for operators), the rules as declared (read-only), the active mutes (a Mute form and End for admins) and the deliveries, filtered by channel and state (ALERTS.md §11). |
| Readiness (`Readiness.tsx`) | For every role, read-only. `#/readiness`: the definition, the settings hash and the notes; per programme `k of n pairs ready` with a step chart of the pairs ready over the last 90 days of recorded transitions; per pair its state (words and a shape), `6 of 9` with a cell a gate, an inline-SVG sparkline of its last evidence cycles (the count against its allowance, its title naming each cycle's suppressed keys; a sampled cycle a pass or fail mark), the evidence cycle, since when it has been in its state, sign-off, pack and deviations; the filters live in the hash query, a download for each `dashboard.pdf` volume and each of the two CSVs, a poll every 60 s. `#/readiness/<name>`: the ladder, the gates by group with the API's texts, convergence, the trend with version markers and each cycle's counts (a sampled cycle's marked as its sample's own) and changes, strata, live soaks, versions, transitions and "Check sign-off now" (`precheck=1`). `#/readiness/<name>/brief`: the pair's cut-over brief (the state and what a brief is; the newest complete cycle's stored figures, each a link into its run; the gates; a two-strip trend of the stored mismatch rate and the root-cause groups over its last 30 measured cycles, lettered `V` or `K` where the spec version or the key set changed; its root-cause groups as `opened`, `still open` or `closed`), with a button for each of `brief.pdf`, `brief.csv` and `causes.csv`. No chart library, and no percentage is built in the UI (`web/src/ready.ts`; READINESS.md §10). |
| Licence (`Licence.tsx`) | For every role, at `#/licence`: the installation id, the licence (unknown, not an evaluation, when the standing cannot be read) and its renewal, its state and warnings, what it allows, the pairs counted with their seats, and who installed or withdrew each licence and why (LICENCE.md §12). In an open build: the id, `This build needs no licence and limits nothing.`, what it allows, and no pairs (§19). |

LLM text is rendered as escaped text, never HTML. Controls a role lacks are hidden; the server still
decides. Each licence warning is a banner on every screen, linking to the Licence screen, and the controls
the licence refuses (Start, Compare now, a licence-paused resume, Request sign-off, Issue evidence pack)
are disabled with its first warning as the reason (`web/src/licensing.ts`); an open build has no warning
and refuses nothing, so it shows no licence banner and disables none. Each notification banner
(`GET /v1/notifications`'s `banners`: mutes, failing channels, an unreadable secret, a notifier behind) is
shown on every screen the same way, linking to the Notifications screen (`web/src/notify.ts`). A
schedule's presets only write cron text, and every firing's text, banner, warning and note is the
server's (`web/src/scheduling.ts` holds the presets and labels). Every pre-check text and every schema
guard line is the server's (`web/src/prechecking.ts` holds the forms' bodies, the labels and file names). The client
keeps the CSRF token in memory and stores no credential. A credential typed on the Targets form or read
from a curl command, the builder's sample key and its unsaved work are held in memory only
(`building.memory`), in no browser storage and no address, and are emptied at every sign-in and sign-out.

## 9. Deployment

- **`deploy/Dockerfile`** is multi-stage, each base pinned by digest: `node:24-alpine` builds `web/dist`,
  then `python:3.13-slim` runs `uv sync --frozen --no-dev` with uv pinned by hash
  (`deploy/uv-requirements.txt`). The runtime stage holds neither uv nor pip, and runs as `10001:10001`
  (RELEASE.md §5).
- **`deploy/docker-compose.yml`** runs `postgres:16-alpine` (with a healthcheck, terse error logs so
  that no row value is logged, and a rotated log) and `app` (`ds serve`, port 8000 on loopback behind
  a TLS proxy, the stable `hostname: deltasleuth-app`, volumes for the evidence, workspace and `p0`
  directories, config and secrets mounted read-only, the keyring and tokeniser key among them, and
  `extra_hosts: host.docker.internal:host-gateway`, by which a target reaches an app on the host from
  Linux too; the workspace volume also holds the targets declared in the product and their credentials,
  DEPLOY section 7), on
  a network pinned to `172.30.0.0/24` with gateway `172.30.0.1` for `auth.trusted_proxies`. The CLI runs as
  `docker compose run --rm -e DS_OPERATOR=… app ds ...`, exchanging files through `p0`. An optional
  profile `mocks` runs both mock pairs. `app` and `mocks` run `deltasleuth:<version>` with
  `pull_policy: never`, a read-only root filesystem, a `/tmp` tmpfs, no capabilities and
  `no-new-privileges`. Every site change goes in the git-ignored `docker-compose.site.yml` (from
  `docker-compose.site.example.yml`, which changes nothing as copied), which `.env` names;
  `docker-compose.source.yml` builds the checkout once, for `app` and `mocks`, as `<project>:source`
  (`deltasleuth:source` unless `.env` names a `COMPOSE_PROJECT_NAME`, so two checkouts on one machine
  share no image), `deploy/env.source.example` is a source build's `.env`, and `docker-compose.ca.yml`
  runs the `-ca` layer (RELEASE.md §6, [FIRSTRUN.md](FIRSTRUN.md) §2).
- **`deploy/config/`** holds example `deltasleuth.yaml` (a one-machine trial as copied), `targets.yaml`
  and `llm_profiles.yaml` (an OpenAI-compatible gateway; Claude; OpenAI), and
  `deltasleuth.dev.example.yaml` for a source checkout without Docker.
- **`deploy/up.sh`** (FIRSTRUN §3) takes a fresh clone to a running one-machine trial with one command,
  `sh deploy/up.sh [--project NAME] [--port N] [--subnet A.B.C.0/24]`: POSIX `sh`, it creates what is
  missing (the settings, the site file and `.env`, four secrets, an unsigned build of the checkout, the
  database, the containers with the mock services, and a first API token saved to
  `.deltasleuth/tokens/first-run`) and overwrites, edits and deletes no file. No secret or token reaches
  the terminal, a variable, a command line or the environment. It refuses whatever is not a trial of
  its own folder before it creates anything: settings that are not a token sign-in at a loopback http
  address, a `.env` without the source override, a loaded release image, a Compose project started
  from another folder, and Docker volumes whose keys the folder lacks; and, of what the folder holds, a
  `deploy/secrets` that other accounts can open, a link to a file that is not there, and a kept
  `db_password` that an address cannot carry as written. A run ends with its closing block only when
  every step passed, a kept token's `ds token list` among them. The options are a second
  installation's (FIRSTRUN §4), and `--port N` alone moves a first one whose port is taken. It was run
  against a Docker daemon on macOS on 2026-10-05; changes made since
  are seen in the tests only (FIRSTRUN's Checked block), and it is unobserved on Linux.
- **Releases** (RELEASE.md): `tools/release.py build` builds the image from a git archive of HEAD with
  the platform's pinned python manifest, smoke-tests it under the compose hardening, saves it,
  catalogues it with syft (CycloneDX 1.6, SPDX 2.3), scans it with grype and pip-audit, gates the
  findings on `tools/triage.toml` (OpenVEX), signs `SHA256SUMS` with the release key and tags the
  commit; `rescan` re-applies today's database and triage, and `sign` signs advisories, key
  transitions and VEX. The bundle ships the image, SBOMs, scan, the source archive (the deployment kit),
  `LICENSE` and `SECURITY.md`; a site verifies it on the Docker host with OpenSSL or LibreSSL and
  `sha256sum` before `docker load`.
- **`docs/mvp/DEPLOY.md`** is the office guide: install a signed release (or build from source,
  unsigned), configure, sign-in with the IdP, the licence (none in this build), migrate, first run, point it at an OpenAI-compatible
  gateway and the real endpoints, upgrade, roll back and re-scan.

## 10. Tests

- **DB tests:** a session fixture starts `postgres:16-alpine` from the local image
  (`docker run --pull never`, random port). The tests are marked `db` and skip when Docker is
  unavailable, unless `DS_REQUIRE_DB=1` (then they fail, for example in CI).
- **End to end** (`tests/e2e`, AI both on and off), on the mock pairs through the DB-backed runner:
  - exactly the seeded discrepancies (`mocks/*/expected.json`);
  - kill -9 of a worker mid-run followed by a resume loses nothing and duplicates nothing;
  - counters reconcile;
  - soak for 2 cycles lists the outcome changes;
  - reports (CSV and PDF) are generated;
  - the agent answers with valid citations against a mock LLM server (OpenAI-compatible and
    Anthropic);
  - a root cause explained and the run summarised are cited drafts in `report.pdf`'s annex, and an
    explanation that cites another root cause is refused (`tests/e2e/test_api.py`);
  - a cycle past its retention, purged by `ds purge`, answers 410 and closes the chat; and after
    `ds keys migrate`, the upgrade's ANALYZE and VACUUM FULL and a checkpoint, no planted raw key,
    header, chat text, scan point, legacy blob name or body marker is left in `$PGDATA/base` or the
    evidence directory (the canary);
  - in tokens mode a run is labelled unapproved (its view and `report.pdf`), and approval requests and
    decisions are refused;
  - a trial whose targets file names neither service declares both in the product, tries each side,
    saves the builder's form and compares; `ds compare`, a process of its own, then reads the product's
    list (`tests/e2e/test_api.py`);
  - a three-cycle soak notifies its first cycle's gaps once, signed, with its counts; hanging and failing
    channels leave a run as it is without them; a worker killed mid-send delivers at most twice under one
    `webhook-id` (`tests/e2e/test_notify.py`, ALERTS.md §14);
  - the table demo as a five-cycle soak, its seeded differences fixed between cycles 2 and 3, moves its
    pair from not ready to parity ready, and the API, the recorded transitions, the CLI, the CSVs and
    each cycle's run report agree (`tests/e2e/test_soak.py`, READINESS.md §17).
- **Public pairs** (`tests/fixtures/public`, file mode, offline):
  - the specs compile;
  - every key is compared;
  - the counters reconcile;
  - the documented differences appear;
  - missing keys resolve as the targets declare.

  Optional live runs are marked `live_public` and skipped by default.
- **API:** tests with FastAPI's TestClient.
  - Auth modes use `base_url="https://ds.test"`, so `__Host-` cookies round-trip; the clients of a
    plain-http loopback `public_url` use `http://localhost:8000` (and a second installation's
    `http://localhost:18000`), where the client's jar returns no `Secure` cookie and the plain cookies of
    [AUTH.md](AUTH.md) §3's exception E1 round-trip; developer-mode clients
    use a loopback base URL and client, with `X-CSRF-Token: dev`.
  - `tests/unit/auth/fake_idp.py` is an in-process OIDC IdP on `127.0.0.1:0` (discovery, JWKS, the
    token endpoint for each client authentication, sign-out), with controls for claims, key rotation,
    outages, slowness and clock offsets; `sign_in` drives a whole sign-in.
  - `tests/conftest.py`'s `TESTER` is the principal of every call that takes `who`.
  - `tests/unit/api/conftest.py`'s `approved(client, spec_id)` approves a version as people other than
    its uploaders would, so an oidc test can run it; `packs_client` is an oidc client with evidence
    packs, and `signed(client)` a signed-off census cycle on it (`tests/unit/reports/packs.py`).
- **Licences** (LICENCE.md §4, §16, §19): the suite runs as the open build: the session fixture
  `open_build` sets `licence.VENDOR_KEYS = ()`, whatever `SHIPPED_KEYS` holds. `tests/conftest.py`
  generates a P-256 vendor key in memory each session (no private key is committed), which a test of a
  vendor build asks for with the fixture `vendor_keys` (a module's `pytestmark`, or one test), and a test
  of both with `build`, parametrised over `BUILDS`; `licence_file` signs a
  licence, `licensed` and `licensed_once` install one dated from the database's `now()` (`licensed_once`
  none in the open build), and
  `unlicensed` deletes them as an operator with the triggers disabled could. In a vendor build oidc
  clients and harnesses
  install Enterprise (`tier=None`: an evaluation); `repo.insert_licence` stores a past one.
- **Notifications** (ALERTS.md §14): every receiver is local (`mocks/notify.py`: a webhook, Slack and Teams
  receiver, an SMTP sink with an in-memory CA, a recording proxy); the `loopback` fixture fails any
  non-loopback resolve or connect, and `receiving_thread` serves a receiver on a thread of its own for a
  TestClient or a CLI command.
- **Readiness** (READINESS.md §17): `tests/unit/runner/measuring.py` writes ended cycles as the worker
  leaves them and runs a readiness pass; `tests/unit/core/ready.py` builds series and governance for
  the pure evaluator. `test_never_told` (API answers, the dashboard, the CSVs; the run report's section
  and `readiness.csv` in `tests/unit/reports/test_readiness_reports.py`; `ds readiness` in the CLI
  tests) holds every surface to §2. `tests/unit/runner/test_readiness_perf.py` holds §14's targets under
  `DS_PERF=1`.
- **Schedules** (SCHEDULES.md §15): a fake clock throughout (`core/cron` takes instants, `decide` is
  pure, `fire(now=)` is injected), and zone tests that never depend on the host's tz data.
  `tests/unit/runner/scheduling.py` declares schedules and makes a time due;
  `tests/unit/runner/test_schedule_fire.py` holds the firing rules, `tests/unit/core/test_cron.py` the
  cron and DST rules, `tests/unit/api/test_schedules_api.py` every message, the firing texts (golden)
  and `test_never_told`, and `tests/e2e/test_schedule.py` has the real worker start a schedule's runs
  and a killed `ds worker` lose and duplicate nothing.
- **Table pre-checks and the schema guard** (PRECHECKS.md §13): `tests/unit/runner/prechecking.py` holds an
  engine in memory that the worker's reads, the API and the CLI share; `tests/unit/core/test_precheck.py`
  holds the pure figures, verdicts, changes and every pair text (golden), with Hypothesis;
  `tests/unit/connectors/test_tables_figures.py` the golden statements of each engine
  (`tests/golden/engines/*_figures.json`) and that a statement holds only grammar; the container tests
  of `test_postgres.py`, `test_mysql.py` and `test_oracle.py` each figure on real servers;
  `tests/unit/runner/test_precheck_run.py` the worker's reads, and `tests/unit/runner/test_drift.py` the
  schema guard's five looks and its texts (golden) on real PostgreSQL, MySQL and Oracle;
  `tests/unit/api/test_prechecks_api.py` every message and `test_never_told`;
  `tests/unit/test_evidence_boundaries.py` that no evidence, readiness or approval module reads a
  pre-check or the guard's record; `tests/e2e/test_precheck.py` six engine pairs with exact figures and
  `tests/e2e/test_drift.py` a soak whose tables change under it on each engine. Under `DS_PERF=1` both
  time §12's reads, the guard's included; PRECHECKS.md §12 publishes what they measured.
- **The MCP server** (MCP.md §16): `tests/unit/mcp_server/` calls `mcp.tools.call` and the MCP library's
  in-process client in both protocol eras on a real store with a scripted loopback service: each tool's
  exact field names, every tool error in order, the caps and the cut, planted canaries in results,
  errors, log lines and audit details, the audit rows, the rates and slots, and a source gate.
  `tests/unit/api/test_mcp_http.py` drives `/v1/mcp` with plain requests and with the library's HTTP
  client inside the app's event loop; `tests/e2e/test_mcp_stdio.py` starts `ds mcp` in a process of its
  own and `tests/e2e/test_mcp_http.py` reads and compares over the mock pair;
  `tests/unit/deploy/test_mcp_snippets.py` holds the RUNBOOK's client blocks to MCP.md §13's.
  `tests/unit/auth/test_principal.py` holds `Throttle.forget`, which drops a key whose audit row could
  not be written so that the next hit is due one. No test reaches an external host.
- **Evidence packs:** each pack's own `VERIFY.txt` runs step by step under `sh`, with `sha256sum`,
  `shasum -a 256` and LibreSSL where present (`DS_REQUIRE_TOOLS=1` fails rather than skips without
  `openssl`, `sha256sum` or `shasum`), and tampered packs fail the step PACK.md §3.3 names.
- **Releases** (RELEASE.md §13): `tests/unit/test_release_tool.py` drives `tools/release.py` in a
  temporary git repository (`tests/unit/releases.py`) with stub `docker`, `syft`, `grype`, `uv`, `npm`
  and pip-audit that follow a scenario and log argv, cwd and environment, and real git and openssl;
  `test_real_syft_and_grype` runs the approved scanners on a save of the local `postgres:16-alpine`
  (skipped without them, failing under `DS_REQUIRE_TOOLS=1`). `test_release_tamper.py` runs a bundle's
  own `VERIFY.txt` step by step, as the pack tests do, and tampered bundles fail the step RELEASE.md §3
  names. `tests/unit/deploy/test_config_examples.py` holds the Dockerfile, compose files and DEPLOY to
  the pins and the hardening; `test_release.py` holds the key list, SECURITY.md and the triage file to
  each other.
- **Benchmark** (BENCHMARK.md §13): `tests/unit/test_bench_tool.py` drives `tools/bench.py` with a stub
  `docker` and a stub `ds` that log argv and environment, and no database; `tests/e2e/test_bench.py`
  (`e2e`, `db`) runs it at 20k rows a side on a throwaway `postgres:16-alpine`, and at 1M under `DS_PERF=1`.
  It asserts correctness, never a time: the other `DS_PERF` tests keep their budgets.
- **The file reader** (READER.md §15, FILES.md §18): its tests start real reader processes and end
  them. `tests/support/faults.py` is the one switch for how: `DS_FAULTS=1` makes real faults on macOS
  (SIGSEGV sent, and the kept mutant on which DuckDB itself faults), where each is a crash report and a
  dialog on the screen, so the default suite sends SIGKILL there and skips the mutant's three tests; on
  Linux they are made always. `DS_FUZZ=<n>` runs n footer mutants and n page mutants (100 of each by
  default). `DS_IMAGE=<image>` runs `tests/e2e/test_reader_image.py` in a built image (`docker run
  --pull never --network none`), skipped otherwise.
- **UI:** `npm test` (the API client: CSRF, the background header, sign-in events, 410 messages, a
  source gate; the retention wording of the Run screen and chat; the approval labels, the review's
  decision body and the reauth URL; the census, score and pack texts and requests; the licence's
  refusals, pause line and texts; the sample panel's texts; the notification texts, banners and who sees the
  Mute form; which controls the "Labelled accuracy" block offers, which labelling it prints, its
  requests, and that no screen builds a result text; the readiness words and shapes, the sparkline's, the
  step chart's and the trend's geometry, a download for each `dashboard.pdf` volume, and that neither
  readiness file holds a percentage; a schedule's presets as cron text, its form's body, the default
  zone, its labels and requests, and that no screen builds a firing text; a table pre-check's form
  bodies, levels, labels, file names and calls, the schema guard's banner and resume label, and that no
  screen builds a pre-check text or shows a tick), `tsc --noEmit` and `vite build` must pass.

## 11. Table sides (PostgreSQL, MySQL, Oracle and files)

[TABLES.md](TABLES.md), [ENGINES.md](ENGINES.md), [FILES.md](FILES.md) with [READER.md](READER.md) (the
file reader's process, its protocol and the accounting of its ends) and [GRAPH.md](GRAPH.md) are the
binding contracts; this is their map.
- **Targets:** a target with `dsn_ref` (a `file:` reference to a libpq DSN) is a PostgreSQL database. It
  takes only `rate_per_s` and `max_concurrency` besides. A target with `engine: mysql` or `oracle`
  declares its endpoint (`host`, `port`, `database` or `service`, `user`, `password_ref`, its TLS
  members and `allow_private_network`), so every session goes to an address the guard vetted
  (`netguard.vetted`). A database target's capabilities are fixed: a missing row is an authoritative
  404. Every session only reads (a read-only session on PostgreSQL and MySQL; on Oracle, which has
  none, a read-only transaction around every read) and renders values the same way on both sides.
- **Engines** (ENGINES.md; TABLES.md §13): `connectors/tables/engine.py` is the seam (`Engine`,
  `ENGINES`, the neutral errors, which hold only a code). PostgreSQL keeps its statements; MySQL
  (PyMySQL) and Oracle (python-oracledb, thin mode) share `compose.py`'s statements over a set of
  primitives each, and `render.py`'s canonical rendering 1. A pair with a MySQL or Oracle side stores
  each table side's engine, server major and minor version, consistency, TLS, classes and declared
  types in its plan; rows are hashed in the engine when both sides are one engine and declare a column
  alike, and in the worker otherwise. Graph parity, `regex` key maps and `ILIKE` are refused on those
  sides. `ds target check` is the pre-flight (§13 of ENGINES.md).
- **Files** (FILES.md; TABLES.md §14): a target with `engine: files` declares a directory (`root`,
  `settle_s`, `allow_writable`), which must lie under `files.roots_under` and outside the directories
  DeltaSleuth uses; a side on it names its files in `table.file` (a relative `path` of a closed grammar,
  `format: csv | parquet`, a CSV's declared dialect, an optional stamp file, `maxAge` and `partitions`).
  `connectors/tables/manifest.py` lists, pins and hashes the files on a thread pool of its own, each call
  under a deadline; `duck.py` reads them through the process's file reader, a child process
  (`reader.py`) that alone loads DuckDB, locked to those roots and its spill, running nothing but one
  `SELECT` a statement and handing rows back in typed frames. A fault or a memory blow-up of DuckDB's ends
  that child alone: another is started, the run pauses (`Died`) or, once a side's files alone were in
  flight at three such ends, fails (`ReaderDied`) ([READER.md](READER.md) §5 to §9, which also says what
  the reader is not: a sandbox). A run's creation hashes and counts the files and
  stores `consistency: fixed`, counts and the one digest `files_sha256`; each file's name, size, times
  and digest are sealed under the run's data key (`params.pinned`), while the spec's path, the count,
  the bytes of all a side's files and the newest modification time are in the plan in clear. Any change
  of the files that a later check sees fails the run (`FilesChanged`; FILES.md §14 point 4 says what a
  writer of the export directory can do unseen). A CSV's lines that are not rows are counted, never dropped silently; on side a
  they leave the cycle without a census. Graph parity, `regex` key maps, `ILIKE`, `table.schema` and
  table pre-checks are refused on a file side. `ds target check ID --table PATH --format csv|parquet`
  drafts a dialect and prints a manifest (FILES.md §16).
- **Spec:** a side's `table` (schema, name, `keyColumns`, optional `columns` with renames, optional
  `where`) replaces `request`, `select` and `map`; a side may add `typeMap`. A table pairs with a
  table or a service. Two tables' composite key is their key columns' values, judged whole; beside a
  service side it is a multi-part key (§4), the table side listing one key column a part.
- **Key tables** (`runner/keys.resolve`, `tables.key_plan`): `key.source` with `kind: table`, a `target`
  and a `table` (in place of a frame member's `side`) names a table, a view or CSV or Parquet files of
  keys on a database or files target, which need be neither side's. Its `table` is a side's (`keyColumns`
  one a part, `where`, key maps, `file`) without `columns`. A read plans anew (no plan, pin or census of
  it is stored), holds one slot of the target's gate and one snapshot, and streams the keys in key order:
  several rows of a key give one key; a row without a key, a CSV file's line that is not a row of its
  declared dialect (`the key source holds N line(s) that are not rows of its declared CSV dialect: fix
  the export or its csv declaration`), or over 1,000,000 keys, refuses the run.
  Every failure is a `ValueError` holding a code and no value (`the key source could not be read
  (<code>)`, 422). `params.keys_source` is `table`, and the read's record `{basis: "key_table", target,
  relation, rows, keys, at}` is the `key_source` of `create_run`'s audit detail and of a roll's
  `run.cycle`. It is a key list, not a census of a side ([PACK.md](PACK.md) §1.2), and its key text is
  not checked against a mixed pair's table side (a `numeric(10,2)` key column gives `1.00`).
- **Plan:** `create_run` and `compare_one` resolve each side's columns, filter and maps against its
  catalogue into `runs.params["tables"]` (compared columns, `only_a` / `only_b`), then have each
  database plan the side's filter and key maps, reading no row (`LIMIT 0`). A failed connection, a missing table or column, a table the
  role may not read, no column to compare, a listed column without a counterpart, a typeMap column
  not compared, or a filter or key map the database refuses raises ValueError, so the run is refused.
- **Row filters** (TABLES.md §9): `where` in a small predicate language (`core/spec/where.py`) bounds
  the scan's population; per-key reads do not exclude rows outside it but record `selected` (and of
  two rows, read the one it selects), and a key selected on one side only gets a `FILTER_MEMBERSHIP`
  finding.
- **Key and type maps** (§10): `keyMap` `identity`, `prefix_pad` or `regex` maps a key column to the
  canonical key, in SQL for the scan and in Python for per-key reads; `typeMap` converts a compared
  column to a canonical type (`core/canonical/convert.py`) for row digests and per-key rows alike.
- **Per-key reads** (§4, §12): `connectors/tables/` `TableConnector` is a `Connector`: one row is
  200, none is 404, two are `identity_ambiguous` unless the filter selects exactly one; a row counts
  only when its canonical key text is the key's. A worker slot reads up to `READ_BATCH` (200) keys
  of a run whose sides are all tables with one query per side (`prefetch`) and saves them in one
  transaction.
- **Scan** (§5): a batch or soak over two tables, with neither keys nor `key.source`, scans them
  (`runner/scan.py`): each side streams key texts and row digests in code-point order from one
  snapshot, the merge counts identical keys and queues every other key for a per-key read, and
  `ds.scans` checkpoints it every 10,000 keys, so a resume counts nothing twice. A scanned key's
  per-key read confirms it (`rechecked`, no recheck pass).
- **Soak** (§11): each cycle scans anew, in its own `ds.scans` row, due at the cycle's start. The keys
  whose result in the previous cycle is not MATCH (`repo.unmatched_keys`) are queued even when
  identical, so `outcome_changes` lists their return to MATCH; a key that returned to MATCH is not
  queued again.
- **Graph parity** (GRAPH.md): a table pair whose composite key is a link fact adds `graph`
  (`members`, `groups`, `maxComponent`). L1 is the scan of the facts; L2 (`runner/graph.py`) runs at
  the scan's end, compares each group's member set and classifies the affected components, into
  `ds.graph_diffs` and `scans.state.graph`. It adds no result, finding or counter.
- **Counters:** `count_cycle(..., bulk_match=scan.matched)`: identical keys count in `total` and
  `match` as MATCH confirmed `bulk_exhaustive`, and every identity holds.
- **Outputs:** coverage.csv adds the rows read and skipped per side, the one-sided columns, the
  filters, each table side's engine (`engine_a`, `engine_b`) and a file side's lines that were not rows
  and repeated keys (`rows_rejected_a`, `rows_rejected_b`, `keys_repeated_a`, `keys_repeated_b`); the PDF
  has a "Tables" line, which names the engines a plan names and a file side's format, files and those
  counts, and, for graph parity, a "Graph parity" section; `graph.csv`
  and `GET /v1/runs/{id}/graph` hold the L2 rows; the Run screen shows the scan's progress, each
  side's engine, server version and consistency where the plan names them, and graph counts.
- **Schema guard** (PRECHECKS.md §7): `ds.runs.guard` holds each table side's shape beside the plan
  (never in `params`, so `plan_sha256` is unchanged). The guard looks at creation, at each scan pass
  before a cursor opens, at a key-list cycle's start, at a resume and after a refused read; a change that
  breaks the plan pauses the run (`run_paused_schema_drift`), a run that would not compare what its
  version's last baseline run (while none is one, its newest stored plan) compared is created paused
  (refused under `schemaDrift: {mode: pause}`), and any other change warns (pauses under `pause`). It
  fails no run.
- **Table pre-checks** (PRECHECKS.md): before any run, a pre-check of stored table pairs (or of two
  schemas paired by name) reads each side's catalogue and a few aggregate statements inside one
  snapshot (`Engine.relations`, `figures`, `duplicates`; `connectors/tables/figures.py`) and judges them
  in `core/precheck.py`: row counts, null and repeated keys, per column null counts, empties, lengths,
  sums, minima, maxima, true counts and distinct counts, compared as verdicts, never values. It compares
  no row, and is no run and no evidence.
