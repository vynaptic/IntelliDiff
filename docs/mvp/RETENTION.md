# Evidence retention, purge and encryption at rest: contract

This contract is binding. It follows [PLAN.md](PLAN.md), builds on
`docs/architecture.md` §13.1 and D.1, and uses the principals and audit rules of [AUTH.md](AUTH.md).

**Goal:** every raw value stored is encrypted under keys held outside the database and its volumes,
and deleted on a date set by the spec's retention class, by an audited, interruptible purge; existing
data is sealed offline by `ds keys migrate`. **Shape:** envelope encryption. A keyring of
key-encryption keys (KEKs) wraps the data keys in `ds.data_keys`: one per run, evidence keys that
wrap each blob's own data key, and the blob address key. Only `run_keys` holds the raw key, sealed;
everything else joins on `key_masked`. The tokeniser key joins the keyring's custody. Masked data,
`business_code` keys included, stays clear.

| A security reviewer asks | Answer |
|---|---|
| How long is raw PII kept? | 90 days (`standard`) or 365 days (`audit`) after each cycle ends, per spec; raw ≥ findings (one clock). Exceptions: a running soak's last ended cycle, until the next ends (≤ retention + one interval); a held run, until released; runs paused or queued beyond retention are cancelled, `one_shot` runs queued 24 h failed (`purge.abandon`), then expire; blobs, 24 h after their last reference; backups and WAL archives, + B days (≤ 35); stray copies, until the KEK then current is destroyed (a rotation period + B); P0 files, by the DEPLOY cron. |
| How is it purged, including from backups? | `ds purge` (hourly in the worker, or on demand) deletes whole ended cycles. Backups age out with the site's rotation; a restore purges and replays later erasures before the app starts (§9). |
| Is it encrypted at rest? | Yes: AES-256-GCM for bodies, raw keys, headers, scan points and chat; keyed blob names; LUKS2 volumes for the rest. |
| Who holds the keys? | Host administrators and the office vault: the keyring and tokeniser key are `file:` secrets outside every volume and backup. DBAs and backup operators hold ciphertext and tokens they cannot reverse. Custody in a KMS or HSM is deferred (§6.3). |
| Are purges audited? | Yes, per cycle, as are holds, erasures, abandonments, rewraps and the migration, each with its actor. The audit log is never purged; it holds masked values only. |

## 1. Keys

- **Keyring:** one KEK per line, 64 hex; blank lines and `#` comments allowed. The last line is
  current; every line unwraps; new data keys are wrapped as §6.1 says.
  `security/crypto.Keyring.from_file(ref)` reads it with `read_secret_bytes`; errors name file and
  line only (`keyring … line 3: expected 64 hex`, and for a KEK listed twice, whose last line would be
  current but not the last id listed, `keyring … line 3: repeats line 1`).
- **Ids:** `K-` + the first 16 hex of `HMAC-SHA256(kek, b"deltasleuth/kek_id/v1")`, `E-…` likewise
  (same label) for evidence keys (`crypto.key_id`); only ids reach rows, headers, errors and audit.
  `ds keys fingerprint FILE` prints a file's ids, marking `keyring.current` (`crypto.fingerprint`
  returns the file's `Keyring`), to check a vault copy.
- **Production:** `encryption.kek_ref: file:/run/secrets/kek` and `encryption.tokeniser_key_ref:
  file:/run/secrets/tokeniser`, required with `auth`, refused inside `evidence_dir` or
  `workspace_dir`. The tokeniser key is read with `read_secret` (hex, never created); its value, so
  every token, `evidence_id_key` and `login_key`, is unchanged. A leftover
  `<workspace_dir>/tokeniser.key` stops every entry point (`tokeniser.key was moved to
  encryption.tokeniser_key_ref: delete it from the workspace`), checked by `load_settings`.
- **Developer mode:** without `encryption`, `<workspace_dir>/kek` and `tokeniser.key`, created by
  `ds init` (O_EXCL, 0600, mode checked on load).

```
keyring (file: secret; later: KMS/HSM) ── AES-KW ── ds.data_keys
 ├─ run data key 'R-…' ── HKDF per (label, cycle, run) ── AES-256-GCM ── raw keys, headers, scan point, chat
 ├─ evidence key 'E-…' ── AES-KW ── data key per blob (its header) ── AES-256-GCM ── zstd frame
 └─ address key        ── HMAC-SHA256 ── blob names
tokeniser key (file: secret) ── tokens, obs_id key, login-cookie key
```

The address key (32 random bytes) does not derive from the tokeniser key, so that key plus an
evidence listing cannot confirm a guessed body.

## 2. Evidence blobs (`evidence/store.py`)

| Offset | Bytes | Field (format 1) |
|---|---|---|
| 0 | 4 | `b"DSE\x01"`; anything else is refused |
| 4 | 18 | evidence key id |
| 22 | 40 | the blob's data key, wrapped with AES-KW (RFC 3394) by that evidence key |
| 62 | 12 | random nonce |
| 74 | n+16 | AES-256-GCM of the zstd frame; AAD = `b"DSE\x01"` + the 32-byte address digest |

- **AAD** leaves out the key id and wrapped key; a file moved to another address fails the tag.
- **Address** `hk1:<hex HMAC-SHA256(address_key, body)>` at `blobs/hk1/<2 hex>/<64 hex>.dse`.
  `put_blob` accepts `obs.content_hash` as None, `sha256:` of the body (a connector's) or `hk1:` of
  the body (a stored observation's), else `EvidenceIntegrityError`; it returns `content_hash =
  hk1:…` and `obs_id = observation_id(obs, "hk1:…", id_key)`.
- **Reads** check the tag, the declared zstd size and the frame, so they need no address key;
  deduplication and modes are unchanged (a put rewrites a corrupted blob, or one whose key is lacking,
  under the newest evidence key). A leftover `blobs/sha256/` stops the start (§7.2) when the store is
  built: `legacy plaintext remains in <evidence_dir>/blobs/sha256: stop the app and run 'ds keys
  migrate' (docs/mvp/RETENTION.md §7.2)`; `migrating=True` (`ds keys migrate`) skips the check.
- **API:** `EvidenceStore(root, *, id_key, address_key, evidence_keys, max_blob_bytes,
  migrating=False)`, where `address_key` and each of `evidence_keys` (oldest first; the newest
  writes) is a `crypto.DataKey(scope, kek, key)` whose `key` is None while the keyring lacks `kek`,
  gains `address(body)`, `put_body(body) -> address` (stores a body without an observation, for `ds
  keys migrate`), `ensure(observations) -> int` (rewrites missing blobs from bodies still in memory,
  §5.4), `delete(address) -> int` (unlink, fsync the directory; bytes freed), `walk() ->
  Iterator[(address, evidence key id, bytes)]` (the id None when the header is not format 1; `.tmp`
  files skipped), and for the orphan sweep `age(address) -> float | None` (seconds since written) and
  `delete_stale_temps(older_than) -> int`. New error: `EvidenceKeyError` (`blob hk1:1f2e… needs E-3a9c…, whose KEK K-… the
  keyring lacks`; `blob hk1:… needs E-…, which ds.data_keys lacks`; for writes, `the evidence
  address key needs K-…, which the keyring lacks` and `evidence key E-… needs K-…, which the keyring
  lacks`).

**Cost** (measured, cryptography 50.0.1, 4 KiB JSON): a new blob +8 µs CPU (HMAC 0.7, wrap and
encrypt 5, verifying decrypt 2.8) and +90 bytes (74-byte header, 16-byte tag); a deduplicated one +3
µs plus a 2 µs `stat` in `ensure`; the write's two fsyncs cost 0.15 to 2 ms. No blob calls the KEK.

## 3. Raw values in PostgreSQL

### 3.1 Spine and sealed values

`observations`, `results` and `findings` lose `key` and join on `key_masked`, already unique per
cycle and spoken by the routes; `run_keys` holds `key_enc` instead of `key`. A sealed value is
`b"\x01" || nonce(12) || AES-256-GCM(subkey, nonce, plaintext, aad = b"deltasleuth/" + label +
b"/v1\x1f" + context)`, with `subkey = HKDF-SHA256(run data key, info = label + b"\x1f" + cycle +
b"\x1f" + run_id)` (no salt; the cycle in decimal ASCII; context and run id UTF-8):

| Value | Where | label | cycle | context |
|---|---|---|---|---|
| raw key | `run_keys.key_enc` | `run_key` | 0 (`start_next_cycle` copies it in SQL) | `key_masked` |
| headers (JSON) | `observations.headers_enc` | `headers` | its cycle | `obs_id` |
| scan point (JSON list of the key's parts) | `scans.state.after = {"enc": "<base64>"}` | `scan_after` | its cycle | empty |
| chat | `chat_messages.sealed`, JSON `{content, tool_calls}` | `chat` | its cycle | `f"{session_id}\x1f{seq}\x1f{role}"` |

A ciphertext moved to another row, position or role fails its context; to another cycle, its subkey;
to another run, its subkey too, even with the run's wrapped data key copied along (AES-KW binds no
scope, but the run id is in the HKDF info). Nonces are random, so SP 800-38D allows 2^32 invocations
per subkey. A cycle's subkeys seal at most its keys × passes × 2 headers, one scan point per
checkpoint and its chat. The raw keys' `('run_key', 0)` subkey serves the run's whole life: every
manifest key, every key a table scan's checkpoint finds, and a rolling soak's key set again each cycle,
none of it reset by a purge. The bound holds while a run seals fewer than 2^32 raw keys over its life,
far above a realistic run (20,000 keys an hour for 24 years), though a rolling soak of 1M keys at an
hourly interval would reach it after about six months.

### 3.2 `security/crypto.py` (new)

```python
class KeyringError(ValueError): ...  # names file, line or KEK id, never key material
class Keyring:                       # current: str; ids: tuple[str, ...]
    @classmethod
    def from_file(cls, ref: str) -> Keyring                  # refuses a repeated line
    def wrap(self, key: bytes, kek_id: str | None = None) -> tuple[str, bytes]  # default: current
    def unwrap(self, kek_id: str, wrapped: bytes) -> bytes   # KeyringError for an id it lacks
class Sealer:                        # Sealer(key, run_id); subkeys cached per (label, cycle)
    def seal(self, label: str, cycle: int, context: str, plaintext: bytes) -> bytes
    def open(self, label: str, cycle: int, context: str, sealed: bytes) -> bytes  # ValueError("sealed value does not authenticate")
    def seal_keys(self, keys: Mapping[str, str]) -> dict[str, bytes]  # raw -> masked into masked -> key_enc
    def open_row(self, row: Row) -> Row  # adds key, headers, content, tool_calls; drops key_enc, headers_enc, sealed
```

`Context` gains `keyring`, `missing_keks` (KEK id -> data keys it wraps), `kek` (the KEK new data keys
are wrapped under, §6.1), `new_sealer(run_id) -> (kek, wrapped, Sealer)` (a new run takes its id from
`repo.new_run_id()` first) and a cached `async sealer(conn, run_id)`, raising `KeyringError` when the
run's KEK is missing (§6.1). `Context.open(settings, *, oidc, rewrap, migrating)`: `create_app`'s lifespan (the
server) passes `rewrap=True`; `ds keys migrate` passes `migrating=True`. `crypto` also holds `DataKey`,
`key_id`, `fingerprint`, `create_keyring` (developer mode's `ds init`) and `chat_context` (the chat
context of §3.1).

### 3.3 Repo and callers

**Rule:** after 007 no column holds a raw key, header or chat text in clear. Repo functions return
`key_enc`, `headers_enc` or `sealed`, never a column named `key`, `headers` or `content`, so a caller
that skips `sealer.open_row` fails with `KeyError`; `api/deps.shown` drops them. From 006 on the repo
neither writes nor reads the legacy plaintext columns (§7.1), and `get_chat_session` names the
session's masked key `key_masked`. Every repo function taking or returning a key works by
`key_masked`; beyond that:

| Repo function | After 007 |
|---|---|
| `create_run(…, *, run_id, created_by, retention, data_key)`, `_insert_keys`, `start_next_cycle` | `keys` maps `key_masked -> key_enc`; `create_run` inserts the run `run_id` (from `new_run_id()`, which its keys were sealed for), `ds.data_keys` and `runs.retention` |
| `save_scan(…, keys, saved_checkpoint)` | compare-and-set on `(sc.state->>'checkpoint')::int = saved_checkpoint`, writing `checkpoint + 1` (a re-sealed `after` never compares equal) |
| `insert_observations(…, [(pass_no, key_masked, obs, headers_enc)])`; `get_observation(s)`, `unmatched_keys` | write `headers_enc`; return rows with `key_enc` |
| `append_chat_message(…, role, payload, sealer)` | takes the next `seq` under the session's row lock, seals with that context (`crypto.chat_context`) in the run's current cycle, inserts `sealed` and `cycle` |
| `get_chat_session`, `chat_answers` | return each message's `sealed`, `session_id`, `seq`, `role` and `cycle` |
| `lock_blobs(conn, *, exclusive=False)` | the blob lock of §5.4 |

| Caller | Change |
|---|---|
| `runner/runs.create_run`, `compare_one`; `runner/cycle.finish_cycle`; `runner/worker._process` | `seal_keys` under `ctx.new_sealer(run_id)` or `ctx.sealer()`; the worker opens leased rows, and a missing KEK pauses the run (§6.1); `create_run` passes the spec's `retention`; a recheck's jitter, stored in `run_keys.available_at`, derives from `key_masked`, never the raw key |
| `runner/compare.save_passes(…, sealer)` | `mask_key(result.key)` (the FK refuses a mismatch); seals headers; `lock_blobs`, `ensure` (§5.4) |
| `runner/compare.side_state`, `rederive_all`; `runner/cycle.summarise`; `reports/data.observations` | `as_observation(sealer.open_row(row))` |
| `runner/scan.scan` | `before` = opened `unmatched_keys`; `after` sealed, `null` at done; `saved` is `state["checkpoint"]` |
| `api/routes/runs.compare`, `agent/tools._compare_key`; `agent/loop`, `api/routes/chat`, `reports/pdf` | open `get_key_detail` for the raw key; seal chat on append, open on read |

### 3.4 Cost per key

Seal 1.6 µs, open 0.6 µs, `mask_key` 0.9 µs, a header seal 2 µs; HKDF (3 µs) once per subkey.
- **1M-row scan:** no crypto in the merge; 20,000 found keys sealed (32 ms), the previous unmatched
  set opened once (12 ms), against a 6.8 s run (TABLES §7).
- **20,000 differing keys:** about 20 µs a key with both blobs: 0.4 s against 35 s of per-key reads,
  0.15 s against the 6.3 s cycle end (TABLES §12); the §10 gate includes the three new indexes.
- **1M-key manifest:** +1.6 s at `create_run`.

## 4. Retention

### 4.1 Policy and tombstones

- **Class:** the spec's `retention` (default `standard`), copied to `runs.retention`. **Days:**
  `retention.standard_days` (90) and `audit_days` (365), read at purge time. **Clock:**
  `ds.cycles.ended_at`, the database's `now()`; due dates are computed in SQL only.
- **Loader:** `retention` leaves `core/spec/rules._IGNORED`. A new warning, `inline_keys_stored`,
  flags `key.source.kind: list` keys of a class other than `business_code`: stored in clear with the
  spec and never purged, they belong in `ds run --keys` (at each list source's `keys`, nested sources
  included: `inline keys of class account_number are stored in clear with the spec and never purged;
  pass them with 'ds run --keys'`).
- **Tombstones:** `repo.end_cycle(conn, run_id, ended_at=None)` inserts `ds.cycles (run_id, cycle,
  ended_at, counters)` (`ended_at` defaults to `now()`), `ON CONFLICT DO NOTHING`, before the audit
  row, in `cycle.finish_cycle` (after `summarise`, before `start_next_cycle`), `runs.compare_one`
  and `cycle.fail_run`. From 014 the tombstone also holds `readiness` ([READINESS.md](READINESS.md) §4):
  `end_cycle` writes the excluded marker of a one-shot or captured-sample cycle, and the worker stores
  every other cycle's measures there once, reading it `FOR SHARE` against a purge's start (a cycle
  whose purge had started is measured from the tombstone alone).

### 4.2 What expires

| Data | When | How |
|---|---|---|
| raw keys, observations, results, findings, root causes, graph diffs, scan state, chat messages | with the cycle | set-based deletes (§5.3); `llm_calls` (usage only) stay; a sampled key's labels and retries go with its `run_keys` row ([SAMPLING.md](SAMPLING.md) §15) |
| chat sessions, run data key | with a terminal run's last unpurged cycle | deleted |
| adjudications' reviewer sheets, drawn keys and disagreements ([PACK.md](PACK.md) §5) | with the cycle | nulled (`finish_purge`); their hashes, counts and scores stay |
| labellings' sheets (masked keys, observation ids, labels; a labeller's notes are never stored, and no hash of the upload is taken) and drawn items ([ACCURACY.md](ACCURACY.md) §12) | with the cycle | nulled (`finish_purge`: `sheet`, `selection.items`); their salt, hashes, counts, truth source and `accuracy` (the score) stay, and rows are never deleted; the draw, `sheet.csv` and the upload answer 410 from the purge's start. A downloaded pack keeps the attested labelling's labels without keys, item numbers or the sheet's order (the row below) |
| stored evidence packs (`ds.pack_zips`) | with the cycle | deleted (`finish_purge`), listed in `retention.purged`'s `packs`; downloads answer 410 from the purge's start (PACK.md §6) |
| blobs | 24 h after their last reference | GC queue (§5.4) |
| run rows, `ds.cycles`, specs, audit log and its hash chain, pack records (`ds.packs`) | never | tombstones; audit details are masked |
| sample draws (`ds.sample_draws`: salt label, nonce, population, frame counts, strata, selection SHA-256, close notes) and each cycle's judgement (`ds.cycles.sample`) | never | aggregates only, append-only but for one close, so a population's ledger and an attested sign-off outlive purges and erasures ([SAMPLING.md](SAMPLING.md) §9, §15) |
| each cycle's readiness measures (`ds.cycles.readiness`: counts, ids, codes, stratum labels and the words of its basis) and a pair's recorded transitions (`readiness.changed` audit rows) | never | kept with the tombstone, like `sample`, so a pair's trend and state outlive purges and erasures and no readiness route answers 410; no key, masked or raw, and no value ([READINESS.md](READINESS.md) §12) |
| schedules (`ds.schedules`: a name, a spec, run options, cron text, a zone name, times, actors and each newest settlement's closed codes) and their audit rows (`schedule.*`) | never | rows are never deleted: a deleted schedule leaves its tombstone, which its runs still name; no key, masked or raw, and no value, so no purge or erasure reaches one ([SCHEDULES.md](SCHEDULES.md) §7). A scheduled run and its cycles expire as any run's |
| table pre-checks (`ds.prechecks` and their `ds.precheck_pairs`: targets, relation and column names, declared types, counts, length ranges, verdicts, closed codes and sums shown; never a key, a text, a minimum, a maximum or a distinct value) | `standard_days` after the pre-check was made | the purge pass's last step, 1,000 a transaction, the pairs cascading (`repo.prune_prechecks`; [PRECHECKS.md](PRECHECKS.md) §8); no hold or erasure applies, a pre-check holding no key and no value; its audit rows (`precheck.*`) stay |
| the schema guard's record of a run (`ds.runs.guard`: each table side's relation and column names, declared types, classes, nullability, primary key and the changes between them) and its `schema.drift` rows | never | it is part of the run row, kept with the tombstone, and holds no key and no value ([PRECHECKS.md](PRECHECKS.md) §7) |
| a draw's temporary table of frame digests | with the draw's session | `CREATE TEMP TABLE`, keyed by the secret nonce |
| a file side's pins (`ds.runs.params.pinned`: each file's relative path, size, both times, inode and SHA-256, sealed under the run's data key: [FILES.md](FILES.md) §6, §13) | with the run's data key | unreadable once the key is deleted; the sealed text stays in the run row. The side's `files_sha256` (one digest of all its files, unkeyed), counts and times stay with the run row, never purged |
| the file reader's spill (`files.spill_dir`, default `<evidence_dir>/spill`: a sort's keys and compared values across engines, keys and hashes between two file sides, plaintext) | when its statement ends; a crashed process's at the next `ds` command's start, or within the hour under a worker | DuckDB removes its temporary files; `manifest.sweep` removes the directory of a process that ended, and logs one it may not remove (left by another user: `spill directory 2 of 5 not swept`), which stays until it is removed by hand; a discarded reader's directory goes when its last statement returns. No purge, erasure or crypto-erasure reaches it before |
| notification deliveries (`ds.notify_deliveries`: rendered bodies of ids, counts and codes; an e-mail's without addresses) and mutes (`ds.notify_mutes`) | `standard_days` after a delivery was made, and after a mute ended or lapsed | the notifier's hourly housekeeping, 10,000 rows a batch ([ALERTS.md](ALERTS.md) §5, §13); the cursor (`ds.notify_cursor`) stays; none is erased with a run, and packs neither cite nor include them |
| every top-level entry of the P0 folder, whatever its name: samples (raw bodies named by raw key), `--missing-keys` and `--keys` files, the profiler's output (its draft spec lists sampled raw keys), specs, output folders | after `standard_days` | the site's cron (DEPLOY §10); the dry run names expiring runs' `samples_dir` |
| downloads, SIEM lines (those of an MCP host-channel container hold tool results: §11), db logs, the LLM provider's copy | not managed | the site's policy |
| downloaded evidence packs | not managed | the site's archive: a pack escapes purge and erasure; its only per-key content is masked keys: the attested sheet's ≤ 500, the key of each `key.view` audit row of the run (a row each time a viewer opened a key, in any cycle) and each AI answer's of the cycle (PACK.md §2.3). A format-3 pack's `accuracy/labels.csv` names no key, but its reader can confirm which of those masked keys a labelling drew (the pack holds the draw's salt and `selection_sha256`) and so a drawn key's label, basis and reason wherever the labels of its population, root cause and `selected_for` are all alike ([ACCURACY.md](ACCURACY.md) §9): to that extent a labelling's link from key to label outlives the cycle's purge in a downloaded pack |

### 4.3 Live, abandoned and held runs

- A cycle without a `ds.cycles` row is live and never due. A live run's last ended cycle is kept
  (the next scan re-reads its unmatched keys; `outcome_changes` compares with it) until the next
  cycle ends. Expiry never fails a soak.
- **Ceiling:** each pass first calls `runner/purge.abandon(ctx, who)`. A `one_shot` run `queued` for 24 h is
  failed (`fail_run` with `AbandonedRun`, which audits `run.finished`), then audited `retention.abandoned`
  in a transaction of its own. A run `paused` or `queued` longer than its retention days (since
  `runs.updated_at`) is cancelled in one transaction, without `summarise`, so it needs no key (a run paused
  for a missing KEK is one): `set_run_status('cancelled', when=(its status,), stop_reason='abandoned')`,
  `cancel_keys`, `end_cycle(ended_at=runs.updated_at)`, then the audit rows `cancelling` and `run.finished`
  (`{status: cancelled, reason: abandoned}`) and `retention.abandoned`. A run paused for a missing four-eyes
  approval (`run_paused_approval`: a revoke, or a soak's boundary; [APPROVALS.md](APPROVALS.md) §2, §4) is
  one too, and so is a soak paused at its boundary for the licence (`run_paused_licence`,
  [LICENCE.md](LICENCE.md) §8), and a sampled run paused by a refused draw (`run_paused_draw`) or at a
  boundary after a draw that did not pass (`run_paused_sample`, [SAMPLING.md](SAMPLING.md) §5); a hold keeps
  its cycles, not the run. A drawn cycle it ends stores its judgement, read from stored rows
  (`sample.ending`). Its cycle is due at once, and a worker's sweep, which never sees the run `cancelling`,
  cannot date it later. A run that moved meanwhile is skipped; one that fails is logged (`run R-… not
  abandoned: <type>`) and left to the next pass, which goes on with the others. A scheduled run is an
  ordinary run to all of this ([SCHEDULES.md](SCHEDULES.md) §7): while one a schedule started is
  `queued`, `running`, `paused` or `cancelling`, the schedule starts no other (`overlap`), and a paused
  one that `abandon` cancels ends that overlap, so the next due time can start. A run the schema guard
  paused (`run_paused_schema_drift`, [PRECHECKS.md](PRECHECKS.md) §7) or created paused is one too: it
  waits for a person to resume or cancel it, and `abandon` cancels it past its retention days as any
  paused run, ending its schedule's `overlap`.
- `ds purge hold R-… --reason TEXT` sets `runs.hold_reason`, `hold_by`, `hold_at`; `release` clears
  them (`repo.set_hold`). The reason reaches the SIEM, so the help forbids personal data. Holding a held
  run or releasing one not held is refused (`run R-… is already held`, `run R-… is not held`). A hold
  placed while a pass or an erasure runs keeps every cycle whose purge has not started (§5.3).
- **Sign-off holds** (APPROVALS.md §2). A sign-off's last approval step holds its run with the reason
  `signed off: A-…`, by the approver (`retention.hold`), unless the run is held already, so that what
  the sign-off attests can be rebuilt. A revoke that leaves the run without an approved sign-off
  releases that hold (`retention.release`, by the revoker). `ds purge release` lifts it like any hold;
  a purge (or erasure) of a signed cycle then revokes its sign-off (`cycle 2 purged`, by the purger)
  in the transaction that starts the cycle's purge (§5.3 T0), before `retention.expire`, unless an
  issued evidence pack cites it: that sign-off stays approved, as the pack certifies it, and the purge
  deletes only the pack's stored ZIP ([PACK.md](PACK.md) §6).

### 4.4 Expired reads

From `purging_at`, `api/deps.live_cycle(conn, run, cycle)` and `reports/data.load_run` raise
`runner/purge.CyclePurged` (through `purge.refuse_purged`), not a `LookupError` (routes map those to
404); an app-level handler answers 410, and the CLI's `_EXPECTED` lists it. A missing blob raises
`runner/compare.EvidenceMissing` (from `compare.body_of`, which every read of a stored body uses),
handled the same way. The date is `purging_at`'s; the parenthesis is the purge reason, or `erasure`
for any `erasure: …` reason.

| Where | Answer |
|---|---|
| a cycle's run routes, `POST /compare` with `run_id`, `ds report`, `ds measure` | 410 (CLI `error:`) `cycle 3 of run R-… was purged on 2026-12-26 (retention standard): its keys, results, findings and evidence are deleted` (or `erasure`) |
| chat, once the run's current cycle is purged | 410 `the run's evidence was purged on 2026-12-26; AI answers need it` |
| `GET /runs/{id}` | adds `retention: {class, days, hold, purged, last_purged_at, cycles: [{cycle, ended_at, expires_at}]}` in place of the run's `retention` column: `hold` is `{reason, by, at}` or null, `purged` the cycles whose purge started, `last_purged_at` the latest `purged_at`, `cycles` the ended cycles not purged (`expires_at` computed in SQL) |
| a missing blob of a live cycle | 410 `evidence of O-… is missing (purged or lost)`; the agent gets a `ToolError` |
| a missing KEK | 503 `evidence unreadable: the keyring lacks K-… (docs/mvp/RETENTION.md §6.1)`, or with an `EvidenceKeyError`'s text (§2) after `evidence unreadable: `; `chat.ask` is not audited when the run's key is missing, as nothing was stored |

`report.pdf` states `Evidence retained until 2026-12-26 (retention standard)`, or while the cycle is
live `Evidence retained until 90 days after the cycle ends (retention standard)`, and for a held run
`Evidence held until released (retention standard)`. The UI (`web/src/retention.ts`) shows the same
line for the Run screen's cycle (a held run's adds who held it, since when and why); a purged cycle
gets the banner `Cycle 3 was purged: its keys, results, findings and evidence are deleted.` in place of
its tables, which would answer 410; and once the run's current cycle is purged, the chat's input is
disabled with `The run's evidence was purged; AI answers need it.`, and its stored session is not
read. Other 410s (a key view, a report, a compare) show the server's message.

## 5. Purge (`runner/purge.py`)

### 5.1 Entry points and rails

- **Worker:** `_Worker._sweep_once` starts a pass at most hourly (`self.purging`, like `scans`), as
  `system:worker`, but none within 7 days of 006's `applied_at` (`repo.grace_over`), so holds can follow
  an upgrade (`ds purge` has no such grace). The first pass after the worker starts, then one a week,
  also sweeps orphans (§5.4); a failed pass is logged and retried the next hour.
- **Table pre-checks** ([PRECHECKS.md](PRECHECKS.md) §8): every pass, the worker's and `ds purge`'s,
  ends by deleting the pre-checks made more than `standard_days` ago, 1,000 a transaction, their pairs
  cascading; the count is `Summary.prechecks`, which joins `retention.finished`'s detail and its condition,
  and `ds purge` prints `deleted N table pre-check(s) past their retention`. An erasure deletes none: a
  pre-check holds no key and no value.
- **One purger:** session advisory lock `0x44535047` ("DSPG") via `pg_try_advisory_lock` on a
  dedicated autocommit connection (as `_sole` does), closed when the pass ends, however it ends. An
  erasure takes it too. The CLI exits 2 with `another purge is running; try again later`
  (`purge.PurgeBusy`); the worker skips the hour.

### 5.2 Due query

```sql
SELECT c.run_id, c.cycle, c.ended_at, r.retention, r.params->>'samples_dir' AS samples_dir
FROM ds.cycles c JOIN ds.runs r USING (run_id)
WHERE c.purged_at IS NULL
  AND (c.purging_at IS NOT NULL                        -- an interrupted purge resumes first
       OR (r.hold_reason IS NULL
           AND c.ended_at + make_interval(days => CASE r.retention WHEN 'audit' THEN %(audit_days)s
                                                      ELSE %(standard_days)s END) <= now()
           AND NOT (r.status IN ('queued', 'running', 'paused', 'cancelling') AND c.cycle >= r.cycle - 1)))
ORDER BY c.purging_at NULLS LAST, c.ended_at
LIMIT %(limit)s
```

### 5.3 One cycle

1. **T0** `repo.start_purge`: `SELECT … FROM ds.runs WHERE run_id = %s FOR SHARE` first, which
   serialises with a hold's `UPDATE` of the row; then, unless the run is held and the cycle not yet
   purging, `purging_at = coalesce(purging_at, now())`, `purge_reason = coalesce(purge_reason, reason)`;
   audit `retention.expire` last, with the stored reason. A cycle whose run was held after the pass read
   the due list is skipped and counted held. The decision is recorded before anything irreversible;
   reads answer 410. A resumed purge keeps the start and reason it had (a pass resuming an erasure
   audits `erasure: …` again).
2. **T1…Tn** `repo.purge_keys(conn, run_id, cycle, lo, lo + 5_000)`: one transaction per chunk of
   `run_keys.seq`, set-based, in dependency order, never through the cascade:

   ```sql
   -- <k>: k.run_id = %(r)s AND k.cycle = %(c)s AND k.seq >= %(lo)s AND k.seq < %(hi)s
   -- <on>: (t.run_id, t.cycle, t.key_masked) = (k.run_id, k.cycle, k.key_masked)
   DELETE FROM ds.findings t USING ds.run_keys k WHERE <k> AND <on>;
   DELETE FROM ds.results t USING ds.run_keys k WHERE <k> AND <on>;
   WITH gone AS (DELETE FROM ds.observations t USING ds.run_keys k WHERE <k> AND <on> RETURNING t.content_hash)
   INSERT INTO ds.blob_gc (content_hash) SELECT DISTINCT content_hash FROM gone WHERE content_hash IS NOT NULL
   ON CONFLICT (content_hash) DO UPDATE SET queued_at = now();   -- the 24 h delay restarts
   DELETE FROM ds.run_keys k WHERE <k>;
   ```

   The observations statement also returns how many rows it deleted and queued (a CTE around the same
   DELETE and INSERT). The NO ACTION checks from `results` on each deleted observation use
   `results_obs_a_idx` and `results_obs_b_idx` (006). Queuing from the deleted rows catches a late
   writer's observation; an erasure also refuses while a key it erases is `leased` with `lease_until >
   now()` (a cycle's keys for `--cycle`, any of the run's for a whole run: a running soak always leases
   its current cycle's keys, which would otherwise block erasing its older cycles).
3. **Final** `repo.finish_purge`, under the run's row lock: delete the cycle's `root_causes`,
   `graph_diffs`, `scans` and `chat_messages`; tombstone its adjudications (PACK §5) and its labellings
   (`UPDATE ds.labellings SET purged_at = now(), sheet = NULL, selection = jsonb_set(selection, '{items}',
   'null') WHERE run_id = %s AND cycle = %s AND purged_at IS NULL`, the one change `ds.tg_labellings`
   allows besides the labels, once, labelled or not: [ACCURACY.md](ACCURACY.md) §12, §13). Only if the run is
   terminal and `NOT EXISTS (SELECT 1
   FROM ds.cycles WHERE run_id = %s AND cycle <> %s AND purged_at IS NULL)`, also delete its
   `chat_sessions` and `ds.data_keys` row, so erasing the last cycle leaves earlier ones readable.
   Set `purged_at`, `purged_by`; audit `retention.purged` last, with this invocation's counts. A crash
   leaves `purging_at` set; the next pass resumes that cycle first. The purging process drops a
   destroyed data key's cached sealer (`Context.forget`).

### 5.4 Shared blobs

- **Drain,** at the start and end of each pass: addresses queued over 24 h ago, 500 per transaction:
  `repo.lock_blobs(conn, exclusive=True)`, `repo.referenced` (via `observations_blob_idx`),
  `evidence.delete` of the unreferenced, `repo.gc_done`. The delay keeps backups consistent (§9).
- **Writers** (`save_passes`, in the transaction inserting the observations): `lock_blobs(conn)`,
  `evidence.ensure(reads)`, insert, commit. Drain first: `ensure` rewrites the file before the
  insert. Writer first: its reference commits before the drain's lock.
- **`lock_blobs`:** one transaction-scoped advisory lock `0x4453424C` ("DSBL"): shared for writers,
  exclusive for a drain or orphan batch (milliseconds).
- **Orphans:** weekly and with `ds purge --orphans`, `evidence.walk()` deletes, under the same lock
  (500 per batch), files over an hour old (`evidence.age(address)`) that are neither referenced nor
  queued (`repo.queued_blobs`), and stale `.*.tmp` files (`evidence.delete_stale_temps(3600)`, as
  `walk()` skips them).

### 5.5 Reaching backups

| Copy | A cycle purged at t |
|---|---|
| live database and evidence | rows at t; blobs 24 h after their last reference |
| dead tuples, WAL, freed blocks | ciphertext, on the encrypted volume, until reclaimed |
| backups, WAL archives, PITR (pgBackRest, Barman) | gone at t + B (≤ 35 days, recorded by the site); those from before `ds keys migrate` hold plaintext |
| copies outside rotation | unreadable once the KEK then current is destroyed in every vault copy (§6.2) |
| the file reader's spill ([FILES.md](FILES.md) §13) | plaintext sort runs while a statement of a file side sorts; a crashed process's stay until the next sweep. Outside `blobs/`, so no purge sees it: the backup command excludes `./spill` ([DEPLOY.md](DEPLOY.md), section 10), and a backup taken without that exclusion while a scan ran carries raw keys and values for B days |

## 6. Key lifecycle

### 6.1 Startup

`Context.open` reads the keyring and tokeniser key (an error stops every entry point), migrates, then
runs `security/keys.start` in one transaction under the migration lock: it creates the address key
and an evidence key if absent (`keys.created`) and loads the address and evidence keys it can unwrap
(run data keys load on first use, through `Context.sealer`). A row that does not unwrap under a KEK
the keyring holds, or an evidence key whose id does not match, stops the start (`KeyringError`: the
row was altered). In `ds serve` only (`rewrap`), it also adds an evidence key when the newest was not
wrapped by the current KEK and rewraps every `ds.data_keys` row under an older KEK (`keys.rewrap`).
Every process wraps new data keys (`Context.new_sealer`, and `ds keys migrate`'s) under `Context.kek`:
the KEK of the store's newest evidence key while its keyring holds it, else the current one. Only the
server's start advances the newest evidence key, so only its restart moves new keys onto an appended
KEK. What holds during a rotation:
- a process started between a KEK's append and the server's restart wraps new run data keys under the
  KEK the running server uses, so the server reads every run it creates;
- a process started before the append and still running after the restart lacks the new KEK: it
  cannot open a data key the restart rewrapped and it had not loaded yet, so its worker pauses that
  run with `run_paused_key_missing` (resume it once the process has ended, as the app's worker then
  runs it);
- a process started before the restart keeps wrapping new keys under the old KEK until it ends, and
  the next server restart rewraps them (§6.2).

`system.start` adds `{retention, keks, current_kek, missing_keks}`.

A missing KEK does not stop the start: its rows stay, with the warning `the keyring lacks K-…, which
wraps 12 data key(s): restore the line from the vault and restart`. Reads needing it answer 503. A
run needing it (its data key, or the address or current evidence key) pauses with
`run_paused_key_missing`, like `AUTH_PAUSE`, when the worker meets a `KeyringError` or
`EvidenceKeyError` reading its keys, scanning or ending its cycle, and hands its leased keys back;
`resume_run` clears either reason once the line is back and the app restarted. Accepting a loss is an
erasure (`--reason "key lost: …"`), which needs no key.

### 6.2 Rotation and a leaked KEK

1. **Rotate** (yearly, or at the site's cryptoperiod): append `openssl rand -hex 32` to
   `deploy/secrets/kek`, vault it, check it with `ds keys fingerprint`, restart the server, best with no
   CLI run in progress (§6.1). The start rewraps every data key (one row per run) and adds an evidence
   key for new blobs; no blob is rewritten. Processes started before that restart keep wrapping new
   keys under the old KEK (step 2).
2. **Destroy** the old line, in the file and every vault copy, B days later, once backups holding
   keys it wraps have rotated out, and only when `ds keys status` shows 0 data keys under it (a
   process started before the restart may have wrapped one; another restart rewraps it).

**A leaked KEK:** rotate at once; cancel and recreate the live runs, whose data keys it opens from
any earlier database copy; destroy the line after B days. With an earlier database copy, its holder
still reads everything sealed before the restart and can test guessed bodies against future
addresses until the address key is replaced (deferred).

### 6.3 A KMS or HSM (deferred)

A KMS or HSM replaces only `Keyring` behind `wrap` and `unwrap` (`kms:` lines, or an HSM KEK). Data keys are
cached, so the KEK is called once per data key per process, never per blob: a 1–5 ms HSM call costs
milliseconds per run.

## 7. Migrations 006, 007 and existing data

### 7.1 `006_retention.sql` and `007_sealed.sql`

006, with 001's default constraint names (asserted by the migration test):

| Table | Change |
|---|---|
| `observations`, `results`, `findings` | drop the FKs and PKs on `key`; add `key_masked` from `run_keys`; drop `key`; PKs and FKs (`ON DELETE CASCADE`) on `key_masked` |
| `run_keys` | PK `(run_id, cycle, key_masked)`, replacing the PK and the unique; `key_enc bytea` (30 to 1053 bytes); `key` nullable |
| `observations` | `headers_enc bytea`; `headers` nullable, without a default; `content_hash` `sha256:` or `hk1:`; `observations_key_idx (run_id, cycle, key_masked)`; `observations_blob_idx (content_hash) WHERE content_hash IS NOT NULL` |
| `results` | `results_obs_a_idx (run_id, obs_a_id) WHERE obs_a_id IS NOT NULL`, likewise `results_obs_b_idx` |
| `scans`, `chat_messages`, `llm_calls` | `state` gains `checkpoint: 0`, `after` null where `done`; `sealed bytea`, `cycle integer NOT NULL` (backfilled below), `content` and `tool_calls` nullable, without a default; `llm_calls.session_id` `ON DELETE SET NULL` |
| `runs` | `retention` (`standard`, or `audit` from the spec); `hold_reason` (1–200 printable characters), `hold_by`, `hold_at`: all or none |
| `blob_gc` (new) | `content_hash` PK (`hk1:`), `queued_at` default `now()` |
| `data_keys` (new) | `scope` PK (`address`, `R-…` or `E-…`), `kek` (`K-…`, indexed), `wrapped` (40 bytes), `created_at`, `rewrapped_at` |
| `cycles` (new) | per ended cycle, never deleted: PK `(run_id, cycle)`, `ended_at`, `counters`, `purging_at` (reads expired), `purged_at`, `purged_by`, `purge_reason` (1–220 printable characters), with pairing checks; `cycles_due_idx (ended_at) WHERE purged_at IS NULL` |

006 then backfills `ds.cycles` with every cycle ended before it (`c.cycle < r.cycle`, or all of a
finished run's), dated by `runs.finished_at` for the run's last cycle and otherwise by the cycle's
latest `results.updated_at`, else the run's start, with `runs.counters` for the cycle they count and
`{}` for the others; and each chat message's `cycle`, as the first cycle whose `ended_at >=
created_at`, else the run's current cycle. The legacy plaintext columns stay until 007, for `ds keys
migrate` to seal; from 006 the app neither writes nor reads them.

007 ends plaintext storage. A `DO` block raises `legacy plaintext remains: stop the app and run 'ds
keys migrate' (docs/mvp/RETENTION.md §7.2)` while a `run_keys` row lacks `key_enc`, an observation
lacks `headers_enc` or has a `sha256:` hash, a chat message lacks `sealed`, or a scan's `after` is
an array. It then drops `run_keys.key`, `observations.headers`, `chat_messages.content` and
`tool_calls`, sets the sealed columns NOT NULL and narrows `content_hash` to `hk1:`. An empty
database takes both at the first start. On a store before 006, where every row of those tables is in
clear, `store/db.migrate` (without `upto`) first evaluates 007's guard against that schema: any
`run_keys`, `observations` or `chat_messages` row, or a `scans` row whose `after` is an array (from
004). Then it raises `MigrationError` with that message before 006 runs, so a refused start (and
compose's restarts) never rewrites the spine only to roll it back; every entry point stops with one CLI
`error:` line, exit 2. Otherwise 007's own guard refuses and the transaction rolls both back.
`migrate(pool, *, upto=None)` applies the migrations up to `upto` only (no pre-check). The migration
test records 006's time per million rows for DEPLOY.

### 7.2 `ds keys migrate`

Offline, resumable and safe to interrupt. It holds the worker lock `DSWR` on its own connection
(refusing `stop the app first: docker compose stop app`), opens the context with `migrate(pool,
upto=6)` and no `blobs/sha256/` check, then runs one-transaction batches selecting only legacy data,
each clearing the plaintext it seals (phases 1 to 5 page through a unique index):

| Phase | Batch |
|---|---|
| 1. data keys for runs without one | 500 runs |
| 2. seal `run_keys.key` into `key_enc` | 5,000 rows |
| 3. seal `observations.headers` | 5,000 rows |
| 4. seal chat (`content`, `tool_calls`) | 1,000 rows |
| 5. seal an unfinished scan's `after` (a paused or failed run's too) | 100 scans |
| 6. per legacy blob file: read and verify it, write its `hk1:` blob (`put_body`), `UPDATE ds.observations SET content_hash = new WHERE content_hash IN (old, placeholder)`, commit, unlink the old file; then each `sha256:` reference left, whose blob is missing | 500 blobs; 500 references |
| 7. delete what is left of `blobs/sha256/`; `migrate(pool)` applies 007 | – |

A legacy blob already missing gets the placeholder `hk1:` + `HMAC(address_key, b"missing\x1f" + old
digest)` (its 32 bytes), which stays absent, so its reads still answer 410; a later run that finds the
blob's file repoints the placeholder too, so it heals. Before anything is sealed, the command refuses
(exit 2, writing nothing) while observations name `sha256:` blobs, none names an `hk1:` one yet
(nothing was moved: after a run interrupted once every file had moved, the rest is truly missing) and
`<evidence_dir>/blobs/sha256` is missing or holds no blob file, as with a wrong `evidence_dir`:
`observations name legacy blobs, but <dir> holds none: check evidence_dir, or run 'ds keys migrate
--accept-missing' to record every one as missing`. `--accept-missing` does so. A corrupted legacy blob
stops the migration (exit 2): `legacy blob <path> is corrupted: restore it from a backup, or delete it to record it as missing,
then run 'ds keys migrate' again`. Ctrl-C exits 130 (`interrupted: run 'ds keys migrate' again to
continue`); an `hk1:` file written before is reused. On a store already at 007 (a new database beside
an older evidence directory) only phases 6 and 7 run. Only `security/legacy.py` reads legacy data; it
goes once no deployment predates encryption at rest. `keys.migrate` is audited, with the invocation's counts, at the
end or an interruption (Ctrl-C or an error); 007's `system.migrate` row dates the end of plaintext
storage. The command prints `sealed: 9 key(s), 14 header(s), 4 chat message(s) and 2 scan point(s)
under 5 new run data key(s); 6 blob(s) re-encrypted, 2 missing; plaintext storage ended (migration
007)`. Estimated: 30,000 rows/s, 2,000 blobs/s.

## 8. Settings, CLI, audit

```yaml
encryption:                       # required with auth; absent: <workspace_dir>/kek and tokeniser.key
  kek_ref: file:/run/secrets/kek
  tokeniser_key_ref: file:/run/secrets/tokeniser
retention:
  standard_days: 90               # 7..3650
  audit_days: 365                 # 7..3650, >= standard_days
```

`config.py` gains closed, frozen `EncryptionSettings` and `RetentionSettings` (`Settings.encryption`
defaults to None). Errors (exit 2): `retention: audit_days must be at least standard_days` (Pydantic
prefixes this cross-field message with `Value error, `, as AUTH §8 notes); `encryption: required with
auth`; `encryption.kek_ref: must not be inside evidence_dir or workspace_dir, whose backups would carry
it` (likewise `tokeniser_key_ref`). The last two are checked by `load_settings` once relative paths
are resolved (a secret's path as `read_secret` reads it, the directories against the settings file).

`ds purge` and `ds keys` are Typer sub-apps like `users_app`:

| Command | Audit | `DS_OPERATOR` |
|---|---|---|
| `ds init` | — | no; without `encryption` also creates the keyring (`… keep a copy of both somewhere safe: a lost key cannot be made again`) |
| `ds purge [--limit N] [--orphans]` | `retention.abandoned`; `retention.expire`, `retention.purged` per cycle; `retention.finished` | yes |
| `ds purge --dry-run` | — | no |
| `ds purge --run R-… [--cycle N] --reason TEXT` | the same, reason `erasure: <text>` | yes |
| `ds purge hold R-… --reason TEXT`, `ds purge release R-…` | `retention.hold`, `retention.release` | yes |
| `ds keys status [--verify] [--json]`, `ds keys fingerprint FILE` | — | no |
| `ds keys migrate [--accept-missing]` | `keys.migrate` | yes |

`ds purge --dry-run` writes nothing and prints `due:`, `held:`, `kept:` (a running soak's last ended
cycle), `abandon:` and `outside retention:` (samples directories) lines, or `nothing to purge`:

```
due: cycle 1 of run R-… (retention standard): expired 2026-12-26
held: cycle 1 of run R-… (retention standard): expired 2026-12-26; held by local:ann@host since 2026-12-01: 'legal hold LH-7'
kept: cycle 2 of run R-… (retention standard): expired 2026-12-26; its running soak reads it until its next cycle ends
abandon: run R-… (queued since 2026-12-25): failed, a one-shot run queued for 24 h
abandon: run R-… (paused since 2026-09-01): cancelled, past its 90 days (retention standard); its cycle is then due
outside retention: /p0/samples-1 (run R-…): P0 samples, for the site's cron
```

`ds purge` prints `purged 3 cycle(s) of 2 run(s): 40,000 key(s); 61,200 blob(s) queued; 58,000 deleted
(1.9 GB), 1,112 kept (shared)`, then, when not zero, `abandoned 1 run(s) past their retention`, `held:
2 cycle(s) past their retention` and `deleted 4 orphaned file(s)`. An erasure of a whole run cancels it
first if live (a restore may revive it): once no key is in flight (checked before the stop, so a
refusal writes nothing), `stop_run` (reason `erasure`, audited `cancelling`), then its cycle ends at
once as `cancelled` (`run.finished`) without `summarise`: its data is erased next, and ending it so
needs no key, so a `key lost: …` erasure works. A key leased between the check and the stop refuses the
erasure too; the worker's sweep then ends the cycle the same way, as `finish_cycle` treats the stop
reason `erasure` like a cancel and skips `summarise` for it. A hold placed while an erasure or a pass
runs keeps the cycles not yet started (§5.3). Refusals (exit 2, no retention row): `run R-… is held`;
`cycle 11 of run R-… is still read by the running soak: erase the whole run`; `the reason holds a
key-shaped value: write it without customer keys` (holds too: audit rows and evidence packs carry
reasons forever, [PACK.md](PACK.md) §2.3); `run R-… still has keys
in flight; try again in 60 s`; `--run needs --reason (an erasure is audited)`; `cycle 4 of run R-… has
not ended`; `cycle 2 of run R-… was purged on 2026-12-26`; `run R-… not found`; `--reason must be 1 to
200 characters without control characters` (holds too); `--cycle and --reason go with --run R-…`;
`--run erases one run: it takes no --dry-run, --orphans or --limit`; `put the options after 'hold'`
(options before a subcommand's name are the pass's, as for `ds users`).

`ds keys status` prints each KEK id with the data keys it wraps, and the missing KEKs; it exits 0
when every KEK in use is present (with `--verify`, also when no referenced blob is corrupted or
unreadable for a missing KEK: the restore check, `security/keys.verify`), else 2 with one `error:`
line (`the keyring lacks 1 KEK(s) in use`, `2 blob(s) do not decrypt`). Missing blobs are counted, not
failed: a missing body answers 410, as a purged one does, and one already missing at `ds keys
migrate` stays missing until its cycle is purged (a restore compares the count, §9):

```
K-3a9c…: 12 data key(s) (current)
K-77d0…: 0 data key(s)
missing K-1f2e…: 3 data key(s); restore the line from the vault and restart
blobs: 1,234 checked: 1,230 readable, 2 missing, 1 corrupted, 1 unreadable (a KEK is missing)
```

`--json` prints `{current, keks: {id: n}, missing: {id: n}, blobs: {checked, readable, missing,
corrupted, key_missing}}` (`blobs` with `--verify` only). `ds keys fingerprint FILE` prints the file's
ids, oldest first, marking `keyring.current` (the last line; a repeated line is refused, §1).

| Audit action | Object | Detail |
|---|---|---|
| `retention.expire` | run_id | `{cycle, reason, retention, ended_at}` |
| `retention.purged` | run_id | `{cycle, keys, observations, results, findings, chat_messages, blobs_queued, data_key_destroyed, packs}`: `packs`, the evidence packs whose stored ZIP it deleted |
| `retention.abandoned` | run_id | `{status, since, retention, days}` |
| `retention.finished` | `purge` | `{cycles, runs, blobs_deleted, blobs_shared, bytes, orphans, held, abandoned, prechecks}`, if the pass or erasure purged, deleted, drained or abandoned anything, or deleted a table pre-check (`prechecks`, from 016: a pass that deleted only pre-checks writes the row too; held cycles alone would add a row every hour of a hold) |
| `retention.hold`, `retention.release` | run_id | `{reason}`; also a sign-off's `signed off: A-…`, by its approver or revoker (§4.3) |
| `approval.revoke` | approval_id | a signed cycle's purge: `{reason: "cycle 2 purged", paused: [], stopped: [], cascade_of: null}`, by the purger (APPROVALS.md §6.4) |
| `keys.created`, `keys.rewrap` (by `system:migration`) | `address`, `E-…`, `keys` | `{kek}`; `{from, to, rows}` |
| `keys.migrate` | `keys` | `{runs, keys, headers, chat_messages, scans, blobs, missing_blobs, complete}` |
| `run.census`, `adjudication.draw`, `adjudication.score`, `pack.issue`, `pack.download`, `pack.revoke` | run_id; adjudication_id; pack_id | [PACK.md](PACK.md) §7.4 (AUTH §6 lists their details) |
| `installation.created`, `licence.install`, `licence.withdraw`, `licence.pair`, `licence.state` | `I-…`; `LI-…` | [LICENCE.md](LICENCE.md) §11 (AUTH §6 lists their details) |
| `sample.draw`, `sample.draw_failed`, `sample.close` | run_id; run_id; draw_id | [SAMPLING.md](SAMPLING.md) §14 (AUTH §6 lists their details); `sample.draw` holds the salt's SHA-256, never the nonce |
| `accuracy.draw`, `accuracy.label` | labelling_id (`V-…`) | [ACCURACY.md](ACCURACY.md) §12 (AUTH §6 lists their details): ids, hashes and counts, never a key, masked or raw, nor a note; a repeated draw and a sheet download audit nothing |
| `readiness.measured`, `readiness.config`, `readiness.changed` | `readiness`; `I-…`; `readiness:<pair>` | [READINESS.md](READINESS.md) §8, §12 (AUTH §6 lists their details), by `system:worker`: ids, states, counts, hashes and slugs; pack segments select by object, so none enters a pack. A readiness report's `report.download` is on object `readiness` |
| `schedule.create`, `schedule.update`, `schedule.pause`, `schedule.resume`, `schedule.delete`, `schedule.fire`, `schedule.missed`, `schedule.rezone` | schedule_id (`SC-…`) | [SCHEDULES.md](SCHEDULES.md) §11 (AUTH §6 lists their details): ids, times, cron text, zone names, slugs and closed codes, never a refusal's message, a key or a value; the writes by their caller, the firings by `system:worker`. Pack segments select by object, so none enters a pack; a scheduled run's `create_run` row, which gains `schedule`, does |
| `precheck.create`, `precheck.finished`, `precheck.cancel`; a pre-check's `report.download` | precheck_id (`PC-…`) | [PRECHECKS.md](PRECHECKS.md) §9 (AUTH §6 lists their details): ids, target ids, schemas as asked, levels, counts and closed codes, never a relation's or column's name, a declared type or a value; kept after the pre-check is deleted. No `PC-` row is in a pack's segment |
| `schema.drift`; `paused` and `running` gain `schema` | run_id | [PRECHECKS.md](PRECHECKS.md) §7, §9 (AUTH §6 lists their details): counts, closed kinds, the basis run's id, an audit seq and 64-hex shape hashes; by `system:worker`, or the caller at a creation or a resume. They are the run's rows, so a pack's segment holds those of its run ([PACK.md](PACK.md) §4.3) |
| `notify.config`, `notify.dead`, `notify.skipped`, `notify.test`, `notify.retry`, `notify.mute`, `notify.unmute` | `notify`; `notify:<channel>`; `M-…` | [ALERTS.md](ALERTS.md) §12 (AUTH §6 lists their details); no detail holds a URL, path, address or secret, and pack segments select by object, so none enters a pack |

No write routes are added: purges, holds and keys stay on the host-administrator channel (AUTH
§4.2). Purges, holds, releases, erasures and `abandon` never consult the licence, whatever its state
([LICENCE.md](LICENCE.md) §2).

## 9. Deployment (DEPLOY)

- **§3:** `(umask 077; openssl rand -hex 32 > deploy/secrets/kek; openssl rand -hex 32 >
  deploy/secrets/tokeniser); chmod 444 deploy/secrets/kek deploy/secrets/tokeniser`, both secrets on
  `app`, the `encryption` block. Vault both, and the keyring after each rotation, checked with `ds
  keys fingerprint`; never beside database or evidence backups.
- **Host images:** a VM or Docker-root backup must exclude `deploy/secrets`, or count as plaintext.
- **§10 volumes:** Docker's data root on LUKS2 (TPM2 or clevis/tang) or a customer-managed-key disk,
  covering WAL, dead tuples and P0 files; swap encrypted or off; no core dumps.
- **§10 logs:** the `db` service runs `postgres -c log_error_verbosity=terse`, so constraint errors
  log no key values; its `json-file` log rotates (`max-size: 10m`, `max-file: 3`). The app's log keeps
  Docker's default driver, which carries the SIEM copy (for `docker compose run --rm` containers too,
  whose `json-file` log goes with them), and rotates there (journald, or `log-opts` in `daemon.json`).
  Under the MCP host channel (`docker compose run … ds mcp --host-channel`, DEPLOY §9) Docker's log of
  that container holds its stdout too, which is the protocol's frames, tool results included: a driver
  that keeps `run --rm` containers' lines (journald) keeps them in the host's log and ships them to
  the SIEM, where no purge or erasure reaches them (§11; [MCP.md](MCP.md) §5.2).
- **§10 P0 files:** everything in the `p0` volume stays in clear: samples (raw bodies named by raw
  key), `--missing-keys` and `--keys` files, the profiler's output (its draft spec lists sampled raw
  keys), specs and `R-…` folders. A daily host cron deletes every top-level entry older than
  `standard_days`, whatever its name, as root in the container since `docker compose cp` copies files
  in as root, with the `DAC_OVERRIDE` the hardened container drops (`docker compose run --rm --no-deps
  -T -u root --cap-add DAC_OVERRIDE app find /var/lib/deltasleuth/p0 -mindepth 1 -maxdepth 1 -mtime +90
  -exec rm -rf {} +`, from the installation's folder, whose `.env` names the compose files). An
  installation from before the site file replaces its earlier line, which names a compose file with
  `-f` and lacks `DAC_OVERRIDE`, and so fails every night and deletes nothing; it runs the new one once
  by hand with `-print` in place of `-exec rm -rf {} +` (DEPLOY §10, "Moving from a source checkout",
  step 6).
- **§10 backups:** `pg_dump`, then `docker compose run --rm --no-deps -T app tar -C
  /var/lib/deltasleuth/evidence --exclude=./spill -cf - .`, ending within 24 h of the dump so the tar
  holds every blob the dump references (§5.4); `ds keys status --verify` runs just before the dump, and
  its missing count is recorded with the backup. B ≤ 35 days, recorded; WAL archives and PITR follow it.
  `--exclude=./spill` leaves out the file reader's spill (§5.5), which holds a sort's keys and values in
  plaintext while a file side sorts: `./spill` is where the default `files.spill_dir` lies, so a spill
  directory placed elsewhere inside a volume that is backed up needs its own exclusion, and one on a
  volume of its own is not in this copy at all ([FILES.md](FILES.md) §13).
- **§10 restore:** keyring and tokeniser key to `deploy/secrets`; `up -d db`, load the dump; untar
  the evidence; `docker compose run --rm app ds keys status --verify` exits 0 and reports the missing
  count no higher than the one recorded with the backup (more means an incomplete evidence copy); `ds purge`; replay from the
  SIEM each `retention.expire` line newer than the backup whose reason starts `erasure:` (`ds purge
  --run R-… --cycle N --reason "<its text>"`, or without `--cycle` when the restored run is still live
  and the cycle is refused as read by its soak or not ended). Only then `up -d app`.
- **Upgrade:** `pg_dump`, and free disk of at least 2.5 to 3 times `observations`, `results`,
  `findings` and `run_keys` (006 rewrites the first three, `ds keys migrate` updates every observation
  and key row again, and VACUUM FULL writes a copy of each), plus WAL headroom (`max_wal_size`, or the
  archive's space with WAL archiving or PITR on); `docker compose stop app`; move
  `workspace/tokeniser.key` to `deploy/secrets/tokeniser`; add the keyring, secrets and `encryption`;
  `docker compose run --rm -e DS_OPERATOR="$(id -un)" app ds keys migrate` (DEPLOY §9's alias);
  `ANALYZE ds.scans, ds.observations` (the tables whose columns `ds keys migrate` rewrote in place:
  their live statistics still hold raw scan points and the legacy blobs' SHA-256 names), then
  `VACUUM FULL ds.run_keys, ds.observations, ds.results, ds.findings, ds.scans, ds.chat_messages,
  ds.runs, pg_catalog.pg_statistic` as a separate statement (dead row versions; the replaced and the
  dropped columns' statistics; `ds.runs` rows still carrying 003's dropped `scan` column with a raw
  resume point; a superuser's, as the compose file's database user is); `up -d app`; within the 7-day
  grace, `ds purge --dry-run` and holds; label older backups plaintext.

## 10. Tests

No image is pulled; PostgreSQL stays the local `postgres:16-alpine` fixture. Paths are under `tests/`.

| File | Behaviours |
|---|---|
| `unit/security/test_crypto.py`, `unit/evidence/test_store.py` | keyring errors echo no content, a repeated line refused; wrapping under a named KEK; open fails with another label, cycle, context (chat seq or role), run key, run id (the data key moved along) or a flipped byte; a planted body marker absent from the blob; flipped or swapped blobs read corrupted; `put_blob` accepts only None or its body's `sha256:` or `hk1:` hash; `ensure` |
| `unit/store/test_context.py` | keys at start; a CLI started between a KEK's append and the server's restart creates a run the running server opens, and after the restart wraps under the new KEK; a run's data key moved to another run opens nothing |
| `unit/test_config.py`, `unit/spec` | every §8 error; a leftover workspace `tokeniser.key`; `retention` not ignored; `inline_keys_stored` |
| `unit/store/test_migration_006.py`, `test_repo.py` | 001–005 with legacy rows (a three-cycle soak, a scan with `after`, chat across cycles, a failed run): 007 refuses, and a start is refused before 006 runs; empty: both apply; constraint names; both backfills; an unopened row raises `KeyError`; `shown()` carries no sealed column |
| `unit/runner/test_scan.py` | a scan over more than 3 × `BATCH` rows completes in one snapshot, with no restart |
| `unit/runner/test_worker.py` | a recheck's jitter derives from the masked key, never the raw one |
| `unit/cli/test_cli.py` | every §8 command and refusal; `ds keys status --verify` fails on a corrupted blob, not a missing one; `ds keys fingerprint` refuses a repeated line |
| `unit/runner/test_purge.py` | due by class and days; grace; hold; a hold placed after the pass read its due cycles keeps them; dry run writes nothing; erasure refusals, a live run's writing nothing; an erased run's cycle ends cancelled without its key or `summarise`; a live soak keeps its last ended cycle; erasing the last cycle of a failed three-cycle soak leaves cycles 1 and 2 readable, chat included; `abandon`, of a run paused for a missing KEK too, its date kept against a racing sweep, and a failing run stopping no pass; actors; a shared blob deleted 24 h after its last reference, re-queuing restarting the delay; a late writer queued; a crash after chunk k; the writer–drain race; orphans; the lock freed after a failed pass |
| `unit/evidence/test_rekey.py` | `ds keys migrate` interrupted in each phase and rerun: 007 applied, API outputs byte-equal (golden JSON), a missing legacy blob still 410; refused under `DSWR`; refused while `blobs/sha256` holds no blob, then `--accept-missing`, then a rerun with the blobs back heals the placeholders; a rotation rewraps and adds an evidence key; a missing KEK pauses a run until restored |
| `e2e/test_retention.py` | `ended_at` moved back, `ds purge`: 410s, chat blocked, report header. **Canary:** after `ds keys migrate`, the upgrade's `ANALYZE` and `VACUUM FULL` (read from DEPLOY §10) and `CHECKPOINT`, a byte search of `$PGDATA/base` (not `pg_wal`; `pg_ls_dir` and `pg_read_binary_file` as the fixture's superuser) and of the evidence directory (legacy zstd frames decompressed) finds no planted raw key, body marker, header, chat text, scan point (two paused scans, and a scan finished under 003 whose point 004 left in `ds.runs`' dropped column) or legacy blob SHA-256 name (four observations); the same fixture before, at 005 and ANALYZEd (the scans' statistics hold a histogram), finds them |

Existing suites pass with only §3.3's signature changes (`test_store` re-puts with the `hk1:`
hash). **Performance** (`DS_PERF=1`, measured on a laptop, run one at a time):
- TABLES' 1M-row scan 7.0 s (6.9 s before encryption at rest, +1.4%); the 20,000 differing keys 44.0 s on average
  of three runs (42.1 s before, +4.5%, within the 5% gate); graph parity on 1M facts 14.8 s.
- Purge (`tests/unit/runner/test_purge.py::test_a_large_cycle_purges_at_five_thousand_keys_a_second`):
  a 200,004-key cycle with its observations, results and findings purges at about 6,300 keys/s, a
  5,000-key chunk in about 0.8 s, so a 1M-key cycle takes about 2.6 minutes of the hourly pass. The
  per-row checks of the `results` -> `observations` foreign keys take most of the time; they are kept,
  because they guarantee every result's cited evidence exists. The gate is 5,000 keys/s.
- Not measured here: 006's duration on a large store (DEPLOY's upgrade step asks for a dump and free
  disk first).

## 11. Honest limits

| An attacker holding | Can | Cannot |
|---|---|---|
| a database dump or read access | read masked data, `business_code` keys and clear-class values; link a key across runs by token; counts, sizes, timings; inline spec keys; the audit log | read raw keys, headers, chat or bodies; reverse a token |
| the evidence volume or its backup | count blobs, see sizes and times | read or test a body |
| the keyring and the database | open raw keys, headers and chat; test guessed low-entropy bodies (error bodies, small JSON) against `content_hash` | read bodies without the evidence |
| the keyring, the database and the evidence | decrypt every blob, freed blocks included, until the KEK wrapping that copy's keys is destroyed | — |
| the tokeniser key and the database, a dump or the SIEM | recover low-entropy identifier keys from tokens, in the never-purged audit log too (10^10 account numbers take minutes); with an obs id from before encryption at rest, confirm a guessed body | decrypt |
| a database dump or a downloaded evidence pack of a run with a file side, and files it holds or guesses | confirm that those files are byte for byte the export the run compared (`files_sha256`, unkeyed: [FILES.md](FILES.md) §13); a small, guessable export by guessing. Read, in clear, the spec's own path, the count of a side's files, their total bytes and the newest modification time: of a side of one file, that file's size and time, and its path where the spec names it | read the matched files' names, or a file's inode, change time or own digest (sealed; in no pack); of a side of several files, any one file's size, or any file's modification time but the newest |
| the spill directory while a file side sorts, or after a crash until the next sweep | read the sort's keys and, across engines, compared values in plaintext | read anything once the statement ended and its files were removed |
| root, the `docker` group or the process | everything: keys are in files and memory | — |

- Legacy plaintext remains in dead tuples, the planner's statistics and 003's dropped `runs.scan`
  column until the upgrade's `ANALYZE` and `VACUUM FULL` (§9), in freed blocks and `pg_wal` until
  overwritten (the canary cannot see them), and in older backups, WAL archives and db logs.
- Purged data remains in backups for B days, and elsewhere until the KEK then current is destroyed
  (a rotation period plus B). A stray evidence copy stays readable with the live database and the
  current KEK, as an evidence key lives while any blob needs it.
- Live runs' data keys and the address key outlive a leaked KEK until the runs are recreated and the
  address key replaced (deferred).
- A writer to the database or the disk can delete rows or blobs (reads answer 410), change masked
  data, or repoint an observation's `content_hash` to another existing blob, whose body it then
  shows (obs ids are not re-verified on read). It cannot forge, edit or move a sealed value (to
  another row, cycle or run, the run's wrapped data key copied along included) or a blob without
  failing its tag. Audit rows are checked against the SIEM copy (AUTH §6), and their hash chain by
  `ds audit verify` ([PACK.md](PACK.md) §4): a consistent rewrite by the database owner shows only
  against the SIEM.
- A downloaded evidence pack escapes purge and erasure: its only per-key content is masked keys, the
  attested reviewer sheet's ≤ 500, the key of each `key.view` audit row of the run (a row each time a
  viewer opened a key, in any cycle, however many: the chain keeps the rows verbatim) and each AI
  answer's of the cycle, reversible for low-entropy keys by whoever holds the tokeniser key (the table
  above). Its labelled-accuracy labels name no key, yet may be put back on a masked key its reader holds
  where a class's labels are all alike (§4.2).
- The exported files themselves are the site's: DeltaSleuth reads them in place, read-only, and no
  purge or erasure touches them or the mount ([FILES.md](FILES.md) §13).
- An MCP tool result leaves DeltaSleuth's control ([MCP.md](MCP.md) N2): the agent's host decides where
  it goes, and no retention, purge or erasure reaches the copies an agent, its client or its model host
  keep of a run's masked keys, findings and values, or of the clear-declared values and raw
  business-code keys where the settings serve `production_clear_declared`. Once a cycle's purge has
  started, MCP's per-run tools answer its tombstone text as the routes answer 410. What the audit log
  keeps of those reads is `key.view` rows with the masked key (as above), `mcp.read` rows (the tool
  and a count) and `compare_one` rows: which runs' data went to which token's agent through MCP, and
  from when (what the same token read on another `/v1` route leaves that route's rows only).
- Under the MCP host channel one such copy is on the installation's own host. `ds mcp --host-channel`
  runs through `docker compose run`, and Docker's logging driver records that container's stdout, the
  protocol's frames: every tool result, with its masked keys, findings and values, and the
  clear-declared values and raw business-code keys where `production_clear_declared` is served. The
  default `json-file` log goes with the `--rm` container; a driver that keeps such containers' lines
  (journald, which DEPLOY §10 names for the SIEM copy) keeps them in the host's log and in the SIEM it
  feeds. No purge, erasure or crypto-erasure reaches either ([MCP.md](MCP.md) §5.2, DEPLOY §9).
- Unmanaged copies (§4.2) go only by the site's cron and policy. Sizes reveal approximate body and
  exact key lengths.
- Encryption at rest is not access control: until a KMS or HSM holds them the keys are as safe as host administration.

## 12. Deferred, documents, size

**Deferred:** KMS or HSM custody and backup crypto-shredding; replacing
the address key and re-encrypting blobs after a leak; `ds keys lost` (an erasure covers it); a purge
cap and `auto_purge` switch; citation-chip and `coverage.csv` retention polish; obs id verification
on read; per-key erasure; admin routes; raw data expiring before findings; an online
migration. Signed evidence packs are built: [PACK.md](PACK.md), which amends §4.2, §4.3 and §8.
Labelled accuracy is built: [ACCURACY.md](ACCURACY.md), which amends §4.2, §5.3, §8 and §11.
Cut-over readiness is built: [READINESS.md](READINESS.md), which amends §4.1, §4.2 and §8.
Schedules are built: [SCHEDULES.md](SCHEDULES.md), which amends §4.2, §4.3 and §8.
Table pre-checks and the schema guard are built: [PRECHECKS.md](PRECHECKS.md), which amends §4.2,
§4.3, §5 and §8.

**Documents:** DEPLOY §3, §4, §10, §11; RUNBOOK; CONTRACTS §2–§4, §7 (and the UI, deployment and test
lines of §8–§10); TABLES §5 (the sealed scan point); AUTH §4.2 and §6, which this contract's §8
extends; architecture §13.1, D.1; the compose file
(both secrets, the `db` command and its log rotation) and example settings. `pyproject.toml` makes
`cryptography>=46` (locked at 50.0.1 through `pyjwt[crypto]`) a direct dependency.

**Size:** about 1,300 product lines and 1,000 test lines.
