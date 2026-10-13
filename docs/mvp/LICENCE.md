# Licence keys and entitlements: contract

This contract is binding. It builds licence keys on [AUTH.md](AUTH.md), [APPROVALS.md](APPROVALS.md), [PACK.md](PACK.md) and
[RETENTION.md](RETENTION.md).

**Goal:** a vendor-signed file binds a customer, an order, a tier, a pair count and a period of at most a
year to one installation; DeltaSleuth enforces the pairs and the end date and fences SSO, four-eyes sign-off
and evidence packs to Enterprise, offline. **Shape:** 010 writes the database's installation id;
`ds licence install` stores the file in `ds.licences`; each decision re-verifies the rows against vendor
public keys the build embeds, so an install applies at once, everywhere, with no restart, setting or
secret. Only new work is refused; nothing calls out (the black-box principle).

**Amended by §19.** Enforcement exists only in a build that trusts
at least one vendor key, a *vendor build*. This build embeds none (`SHIPPED_KEYS = ()`): it is an *open
build*, which needs no licence and enforces none; its standing is `open`. §1 to §18 are a vendor build's:
they stay, with their code and their tests, and where one of them says
what a build without keys does it is amended in place and marked. §19 states the open build as built.

**Threat model.** An operator can patch a self-hosted Python product: the licence is a contractual
control, not DRM. Breaking it takes a code patch, a database edit with triggers disabled, a clock set back,
a copied database or an older build, each leaving a trace (§18). **Names.** The *installation id* (`I-`
and 16 hex) names the control-plane database; `packs.installation` stays the key
register's name (PACK §3.1) and the manifest's `installation`.

## 1. Decisions

**Rules (binding).** O1: without a licence, evaluation limits: 2 pairs, everything labelled
"unlicensed evaluation", never a pack; development, tests and demos keep working. O2: enforce the pairs and
the end date; fence only SSO (oidc mode), four-eyes sign-off and packs to Enterprise; tiers `community`,
`pro`, `enterprise`. O3: warnings from 30 days before the end; for 30 days after it everything works, with
warnings; then new runs and packs are refused; reads, reports, exports, purge and audit always work.
O4 (over O1 to O3 wherever no vendor key is embedded): no limits and no licence needed: unlimited pairs,
sign-off, evidence packs and SSO runs all work, and the licence code stays in place, switched off (§19).

**This contract's:**

- **D1** The licence lives in the database, installed by `ds licence install` (CLI only, AUTH §4.2): a
  restore carries it, every process agrees. A signed statement, not a secret: the `file:` rule does not apply.
- **D2** The installation id is written once by 010, derived from the first audit chain row (random on a
  store with no audit row, a fresh one): dumps, replicas and PITR keep it, and 010 applied again to a dump
  or PITR point from before it makes the same id; a new empty database is a new environment and a fresh
  evaluation. No hardware binding, which restores would break.
- **D3** Evaluation fences what Community fences: at most 2 pairs, no runs under SSO (M5), no sign-off (M6),
  no pack (M7), in every auth mode (developer mode too, or a no-auth `--config` would run unlimited pairs).
  Sign-in and reads stay open. No paid tier is worse than an evaluation; SSO tests install a licence (§4).
- **D4** The SSO fence refuses work, never sign-in, and judges the installation: this process in oidc mode
  or the newest `system.start` saying `auth: oidc`, so a CLI `--config` without `auth.oidc` cannot escape.
- **D5** The first `pairs` names by first run in a licence's window hold its *seats* (in evaluation the
  first two names ever). A new run, the next cycle of a soak and a resume from a licence pause need the
  name seated; a name admitted is last, so seats never move within a window.
- **D6** A licence covers at most 366 days; a multi-year order gets one per year, issued together.
- **D7** One clock, the database's `now()`, as retention's; a clock set back is shown (`clock_behind`),
  never refused: a high-water mark would let one forward jump expire a licence for good.
- **D8** A run made in evaluation never gets a pack, so its label never contradicts a certificate; a run
  without a record (made before 010, or by an older build) is judged by the licence at issue.
- **D9** `ds licence withdraw` undoes a genuine but wrong file before it starts; a started licence is only
  replaced (§5), so no withdrawal re-dates or ends a licence in force or returns an installation to
  evaluation. Installing resumes nothing: people resume, as after approvals.
- **D10** `GET /v1/health` is unchanged; monitoring reads the SIEM's `licence.state`.
- **D11** O2 fences oidc mode, the only mode with persons: under Community and Pro, runs happen in tokens
  mode, without per-user audit, the Admin screen, spec approvals or the reveal (AUTH §1). The alternative is one line in `Standing.refusal`.

**Sign-off sampling** ([SAMPLING.md](SAMPLING.md) D10, §15) adds no fence: draws and closes work in every
tier and in evaluation; a sampled sign-off stays M6 and its pack M7 and M4. A fence on
sign-off sampling would be one line in `Standing.refusal`.

**Table sides on MySQL and Oracle** ([ENGINES.md](ENGINES.md) §13) add no fence: `licence.admit` reads no
engine, such a pair counts as any pair, and it runs in every tier and in evaluation. A fence by
engine would be one line in `Standing.refusal`. **File sides** ([FILES.md](FILES.md) §16) add none either:
a pair with a CSV or Parquet side counts as any pair.

**Alerts and notifications** ([ALERTS.md](ALERTS.md) D6) add no fence either: every channel, rule, test,
retry and mute works in every tier and in evaluation, and the notifier never calls `licence`; a run made
in evaluation notifies with the `[unlicensed evaluation]` label (O1), and `licence.state` is itself an
event. A fence would be one line in `Standing.refusal`.

**Labelled accuracy** ([ACCURACY.md](ACCURACY.md) D8, §12) adds no fence: a labelling is drawn, labelled
and read in every tier, in evaluation and in every auth mode, and `reports/accuracy.py` never calls
`licence`; the sign-off that attests one stays M6 and its pack M7 and M4.

**Cut-over readiness** ([READINESS.md](READINESS.md) D9, §12) adds no fence: measures, the routes, `ds
readiness`, the reports and `readiness.changed` work in every tier, in evaluation and in every auth mode,
and a key census is counted for readiness there too. The evaluation reads the standing only to say why
a `signoff` or `pack` gate cannot be made (`unavailable`: `sign-off needs an Enterprise licence, which
this installation's does not allow (docs/mvp/LICENCE.md §6)`); a stored sign-off or pack counts whatever
the licence allows now, so an expiry or a downgrade never lowers a pair that holds them. A fence
would be one line in `Standing.refusal`.

**Schedules** ([SCHEDULES.md](SCHEDULES.md) D9, §7) add no fence: a schedule is declared, saved, paused,
resumed, deleted and read in every tier, in evaluation and in every auth mode, and no schedule route or
`ds schedules` command calls `licence`. A firing is `create_run`, so `admit` judges it as any run: what
the licence refuses a person it refuses the worker. A fence would be one line in
`Standing.refusal`.

**Table pre-checks and the schema guard** ([PRECHECKS.md](PRECHECKS.md) D13, §9) add no fence: no
pre-check route or `ds precheck` command calls `licence.admit` or `need`, and a pre-check counts no pair
(it is no run). The one read is `licence.status`, which never raises, to record `evaluation`: a pre-check
made in an unlicensed evaluation carries `evaluation: true` in every API item, prints `Unlicensed
evaluation` under `NOT` on the screen and in `ds precheck`, and its exports are named
`PC-…-unlicensed-evaluation-<name>` and headed the same (a CSV's second record, the PDF's line under
`NOT`). The schema guard's pause of a run, its resume and a run it creates paused change nothing the
licence counts or decides: the run's pair was seated at its creation, as any run's.

**The MCP server** ([MCP.md](MCP.md) D10, §7) adds no fence: its route, `ds mcp` and its ten reads answer
in every tier, in an unlicensed evaluation and in every auth mode, and none calls `licence.admit` or
`need`. `list_runs` and `get_run_summary` show a run's licence label (`licence.run_label`). Its one work
tool, `compare_key`, is `compare_one`, so the runner judges it as any compare (M4, M5); and before the
runner it reads `licence.standing` and refuses a spec name that holds no seat (T18: `compare_key over MCP
seats no pair: spec <name> holds no seat under <licence | an unlicensed evaluation>; a person starts a run
of it first`) unless the pairs are unlimited, so that no MCP call seats a pair. That is the tool's rule,
not the token's: an agent's bearer is a general API token, and an `operator` token's own `POST
/v1/compare` or `POST /v1/runs` seats a new name as any caller's does ([MCP.md](MCP.md) N4, §6). A
fence would be one line in `Standing.refusal`.

## 2. Invariants

| Always works, whatever the licence | Because |
|---|---|
| Sign-in (SSO too), tokens, sessions, admin routes, `ds token`, `session`, `users`; spec approvals, reject and revoke; pause, cancel, and every resume but from a licence pause; spec upload; `ds serve` and `ds worker` start | none calls `licence`; startup reads only `licence.shown`, which never raises |
| Every GET, `/v1/audit` and `/v1/licence` included; chat but `compare_key`; `ds licence` | no standing read but `licence.status` (`GET /v1/licence`, `ds licence`), which answers `state: unknown` on failure; a run and its reports are labelled from its own record (`run_label`, `file_name`) |
| Sign-offs under Enterprise past its grace | O3 refuses runs and packs only; each sign-off's hold (APPROVALS §2) keeps its cycle for a pack after renewal. After a vendor key compromise, an installation whose newest licence is an Enterprise one past its grace, signed by the removed key, is an evaluation, refusing sign-off (M6), until it renews (§4) |
| Holds, release, erasure, purge, `abandon` | `runner/purge.py` never calls it; an erasure's `stop_run` and `finish_cycle`'s cancelling branch have no check |
| `ds audit [verify]`, `ds packs [key\|revocations\|download\|verify]`, `report`, `measure`, `adjudicate`, `score`, `keys` | never read the standing (`report` names its files by the run's record); only `serve`, `worker`, `run`, `compare` print licence warnings; with `packs`, M5 never advises tokens mode, which `load_settings` refuses beside `packs` |
| Work in flight | leases, scans, censuses and key processing never call it: a cycle under way always ends; a soak's next cycle is judged at its boundary as of when it falls due, and a failed check lets it go on (§8) |
| Notifications: the notifier, the notification routes and `ds notify` ([ALERTS.md](ALERTS.md) §13) | none calls `licence`; `notify.daily` reports the newest `licence.state` row's state, never the standing |
| Readiness: the worker's measures and transitions, the readiness routes, reports and `ds readiness` ([READINESS.md](READINESS.md) §5, §12) | nothing is refused: the evaluation reads `licence.standing` in a savepoint, and when it raises, the `signoff` and `pack` gates of a pair without the stored fact keep what the licence last decided of them (`not met` or `unavailable`, with `the licence standing could not be read (see the server log): last known`) and otherwise read `unknown`, never `met`; the dashboard's header prints `licence.status`'s state |
| Table pre-checks: their routes, `ds precheck` and the worker's reads ([PRECHECKS.md](PRECHECKS.md) §9) | none calls `admit` or `need` and no pair is counted, so a pre-check is made, read, cancelled and downloaded past the grace too (`test_nothing_locks_out`); `licence.status` only labels an evaluation's |
| Schedules: the schedule routes and `ds schedules` ([SCHEDULES.md](SCHEDULES.md) §7) | none calls `licence`, so a schedule is created, paused, resumed and deleted past the grace too (`test_nothing_locks_out`); only a firing is judged, by `create_run`'s `admit`, and a refused one is a `schedule.missed {reason: licence}` row, never a failed sweep |

## 3. The licence file

```text
format: deltasleuth-licence/1
licence: LI-01JB8Q7ZK3M9W2XR5T4VNH6D0C
customer: Example Bank plc
order: ORD-2026-014
installation: I-3f2a9c1b7d4e5a60
tier: enterprise
pairs: unlimited
starts: 2026-10-01
ends: 2027-09-30
issued: 2026-09-28T10:00:00Z
key: 077646502f1985bea359f0983aa09b8a446d3d5d9a7d873df55a6006009665c4
signature: MEUCIQC5oHedsk8EWa8WWaTqbBs7fqlJVWHUYyHcL3rKNEkUKwIgLJiHmmW8edG2eDpvX8Fiy3N2RcyOV+DWLJWbnq7F/pI=
```

Exactly these twelve lines, in order: `licence` is `LI-` and 26 of `0-9A-Z` (`licence.new_id()` via
`repo._sortable_id`; `L-` names LLM calls); `customer` (1–200 characters) and `order` (1–100) are printable
(`principal.printable`) and neither begins nor ends with a blank; `installation` is `I-` and 16 lowercase
hex; `tier` one of the three; `pairs` `unlimited` or 1–9999; `starts ≤ ends ≤ starts + 365 days`, inclusive
UTC days; `issued` `YYYY-MM-DDTHH:MM:SSZ`; `key` the signer's `signing.fingerprint`; `signature` base64 of
the DER ECDSA P-256/SHA-256 signature (RFC 6979) of the **body**, the eleven lines before it, each ending
in LF (`licence.body(fields)`), whose first line sets it apart from any other signed text.

`licence.normalise` drops a leading BOM, each line's trailing CR and blanks and every empty line, so
e-mailed files verify (no value ends with a blank, so no signed byte is lost); over 4096 bytes, or text
that is not UTF-8, is refused (I2).
Its result is the **text** stored. `licence.parse(data, keys=None, *, name="the file")` (`name` is I2's
`<file>`) checks, in order: a trusted key exists (I1); the format line (I3 for another `deltasleuth-licence/N`, else I2); the grammar (I2); `key` trusted
(I4); `signing.verifies(pem, signature, body)` (I5). Messages echo only accepted values. Offline, with
stock tools (checked with BSD awk on OpenSSL 3.6 and LibreSSL 3.3, on a file with a BOM, CRLFs, trailing
blanks and empty lines):

```sh
LC_ALL=C awk '{ sub(/\r$/, ""); sub(/[ \t]+$/, ""); if (NR == 1) sub(/^\357\273\277/, ""); if ($0 != "") print }' licence.txt > l.txt
sed '$d' l.txt > body.txt
sed -n 's/^signature: //p' l.txt | openssl base64 -d -A > sig.der
openssl pkey -pubin -in vendor.pem -outform DER | sha256sum          # or shasum -a 256; equals key:
openssl dgst -sha256 -verify vendor.pem -signature sig.der body.txt  # Verified OK
```

## 4. Vendor keys

`licence.VENDOR_KEYS: tuple[bytes, ...]`, public PEM keys (SPKI, P-256) read at call time
(`signing.load_public`), is the only trust root: no setting, environment variable or file adds one. As
amended (§19): while it is empty the build is open, every install is refused (I1) and nothing is
enforced. `tests/fixtures/vendor-keys.txt` lists each
production key as `<fingerprint> trusted` or `<fingerprint> removed YYYY-MM-DD`; a test fails unless
exactly the trusted ones are embedded in `SHIPPED_KEYS` and the removed ones, with their days, in
`licence.REMOVED_KEYS: dict[str, str]` (`{"<fingerprint>": "YYYY-MM-DD"}`), and `test_release.py` fails a
version without `.dev` trusting exactly one (as amended: none is an open build's release, §19; a vendor
build's trusts an issuing key and a standby).

- **Bootstrap** (the maintainer, once, before the first release of a vendor build; as amended, an open build's
  release needs none, §19), on an offline machine with a checkout (`uv sync`):
  1. **Generate two keys**, K1 to issue and K2 a standby vaulted apart, each in a directory outside every
     git working tree (`dsv` refuses one inside):

     ```sh
     uv run python tools/dsv.py keygen --out /offline/K1   # asks for a passphrase twice,
     uv run python tools/dsv.py keygen --out /offline/K2   # or --passphrase file:/absolute/path
     ```

     Each writes `vendor-<fp16>.key.pem` (P-256, PKCS#8, encrypted, mode 0600) and `vendor-<fp16>.pub.pem`
     and prints the key's fingerprint, its `SHIPPED_KEYS` entry (a bytes literal, ready to paste) and its
     `vendor-keys.txt` line (`<fingerprint> trusted`).
  2. **Embed the public keys** in the checkout: paste both entries inside `SHIPPED_KEYS: tuple[bytes, ...]
     = (…)` in `deltasleuth/licence.py` (`VENDOR_KEYS` is `SHIPPED_KEYS`, the only trust root), add both
     lines to `tests/fixtures/vendor-keys.txt`, and list both fingerprints in the table below, for sites'
     security reviews. `test_shipped_keys_match_fixture` fails until the tuple and the file agree, and
     `test_two_vendor_keys` fails a version without `.dev` that trusts exactly one (as amended: none is
     an open build's release, §19, and passes, so a paste lost from both the tuple and the file is not
     caught here: `release build` then asks the notes for the open build's line, which a vendor build's
     notes do not hold).
  3. **Commit** those three files, public keys only (`test_no_private_key_tracked` fails a tracked private
     key; `.gitignore` ignores `*.key.*`); release, with notes that do not hold the open build's line
     (as amended: `release build` refuses a vendor build's notes with it, §19); upgrade each installation (§15),
     whose licence the maintainer issues
     with `dsv issue --key file:/offline/K1/vendor-<fp16>.key.pem …` (§14), which refuses a key the
     checkout's `SHIPPED_KEYS` lacks.

  | Key | Fingerprint | Trusted since |
  |---|---|---|
  | — | none: an open build (§19), in which every install is refused (I1) | — |
- **Rotation:** a release adds K3 as standby, K2 issues, K1's private key is destroyed and its public key
  stays, so old licences verify in later builds. **Loss:** K2 issues at once. **Compromise:** reissue under
  K2 what K1 signed that is running, in its grace or ahead (the register, §14), each under a new id (I8),
  then release without K1 (its entry leaves `SHIPPED_KEYS`, its line becomes `<fingerprint> removed
  YYYY-MM-DD` and `REMOVED_KEYS` gains it with that day), noting "install your reissued licence before
  upgrading"; older builds still trust K1. In that release a row K1 signed grants nothing and is not warned
  of: it is logged once (INFO), and `GET /v1/licence`, `ds licence --json` and the Licence screen give its
  `problem` as `signed by key 0d41a9ce0b6b…, withdrawn on 2027-01-05 after a compromise; its reissue
  replaces it`; a row signed by a key the build never knew is still warned (§10). A licence past its grace
  cannot be reissued (install refuses it, I7): an installation whose newest licence is a K1 licence past its
  grace becomes an unlicensed evaluation after the upgrade and loses sign-off past the grace (M6) until it
  renews, its holds keeping the cycles.
- **Tests** (as amended, §19): `tests/conftest.py` generates a P-256 key in memory. The build under test
  is the open one: the session autouse fixture `open_build` sets `VENDOR_KEYS = ()`
  (`pytest.MonkeyPatch.context()`), whatever `SHIPPED_KEYS` holds, which it keeps. A test of this
  contract's §1 to §18 asks for the fixture `vendor_keys`, which sets `VENDOR_KEYS = (pem,)` for that
  test (a module's `pytestmark = pytest.mark.usefixtures("vendor_keys")`, or the mark on one test); a
  test of both asks for `build`, parametrised over `BUILDS` (`indirect`). `licence_file(
  installation_id, *, tier="enterprise", pairs=None, starts=None, ends=None, key=None, **fields)` signs one
  (`fields` replace other lines' values by name: `licence`, `customer`, `issued`, …),
  `licensed(ctx, **fields)` installs one (I1 in the open build), `licensed_once(ctx, tier="enterprise")`
  one of `tier` unless the store names one (`tier=None`: none) or the build is open, which needs none, and
  `repo.insert_licence` stores a past one (grace, ended) that
  I7 refuses; `unlicensed(ctx, *, ended=None)` deletes every stored licence with the triggers disabled, as
  §18's operator could, then stores one of tier `ended` whose grace ended yesterday. No private key is
  committed. The one subprocess test, `test_kill_resume` (a batch in
  developer mode), sees no test key: an open build.

## 5. Installation, storage, the licence in force

**The id.** 010 inserts one append-only row, `I-` and 16 hex of the SHA-256 of `deltasleuth-installation`,
a line feed and the `hash` of the first `ds.audit_chain` row (PACK §4), or of a `gen_random_uuid()` when
the chain is empty; `store/db.migrate` audits `installation.created` when 10 applies, as it audits
`spec.rehash`. The row is table data, so DEPLOY §10's dump loaded into the empty database keeps it, as a
`CREATE DATABASE … TEMPLATE` copy does (checked, §13); without the row every licence is another
installation's: evaluation, which blocks no read. The first chain row is append-only and the same in a
dump or PITR point taken before 010 (one before 009 too: 009 chains the same first row again), so 010
applied again to such a restore makes the same id and the licences installed since install on it
(checked, §13); a copy made before 010 is its source's installation, as one made after is. The chain is
empty only when the audit log was: a fresh database, whose first `migrate` applies 010 before it audits
anything, gets a random id, which only a backup taken after 010 keeps. Customers read it with `ds licence
id` (the id alone on stdout, and the first command of the upgrade, §15), `ds licence`, the Licence screen
or `GET /v1/licence`.

**Install and withdraw.** `licence.install(ctx, data, *, who, name)`: `parse` (I1–I5); then one
`repo.audited` transaction that first takes DSLC (§7), so installs serialise, and reads the standing
before: this installation (I6), not past its grace (I7); the same id and `sha256`, not withdrawn, writes nothing and exits 0 (`licence LI-… is installed
already`, so restores may repeat it); withdrawn, I9; other content, I8; else it inserts the `text`,
`sha256` and `installed_by`, and audits `licence.install` last. `licence.withdraw` refuses a licence whose
`starts` has come (W1), else sets `withdrawn_by`, `withdrawn_at = now()` (the trigger refuses any other
time) and `withdraw_reason` once; W1 is judged by the withdrawal's own `now()`, rolled back when it refuses,
so the day checked is the day written. Rows are never deleted.

**Reading.** `licence.standing(ctx, conn)` runs at each decision: `repo.licence_facts(conn)` (the id,
`now()`, the newest run's `created_at`, the newest `system.start`'s `auth` and `pack_key` and each
`licence.install` object with its audit id, via `audit_log_action_idx`, and the rows), then the pure
`licence.judge(facts, now, auth_mode, packs=…)` with `now = facts["now"]` (or `at` when later:
`standing(ctx, conn, at=…)` judges a soak's cycle as of when it falls due, §8) and `packs` whether this
process has `packs` (M5's tail). Texts are parsed once per `sha256`; a row
that fails verification (or cannot be read at all: `its text cannot be read (<type>)`) or names another
installation grants nothing and is logged and warned, one signed by a key in `REMOVED_KEYS` logged once and
not warned (`Standing.retired`, §4), and a `licence.install` without its row is warned. No stored row makes
a decision raise. With `today` the UTC date of `now`:

```text
good    = rows that verify, name this installation and were not withdrawn before their starts
running = good, starts <= today <= ends     -> latest starts, then latest issued
graced  = good, ends < today <= ends + 30  -> latest ends, then latest issued
ended   = good, ends + 30 < today          -> latest ends
named   = running, else graced, else ended, else none (evaluation)
```

A renewal installed early waits for its `starts`; a later-starting replacement (upgrade, downgrade,
correction, trial) takes over then, a same-day one by `issued`, once installed: nothing reaches an
air-gapped installation otherwise (§18). A **like-for-like renewal** (covering the day after the running
licence's `ends`, ending later, tier and pairs at least its own) silences `ending`; a lesser one is named in
it.

## 6. The standing

| State | When (`ends` 2027-09-30) | New runs | Pairs | Runs under SSO | Sign-off | Packs |
|---|---|---|---|---|---|---|
| `evaluation` | none named | yes | first two names ever | no (M5) | no (M6) | no (M7) |
| `valid` | running, before `ends − 30` or renewed like-for-like | yes | the licence's seats | Enterprise (M5) | Enterprise (M6) | Enterprise (M7) |
| `ending` | running, 2027-08-31 to `ends` | as `valid` | | | | |
| `grace` | graced, 2027-10-01 to 2027-10-30 | as `valid` | | | | |
| `expired` | ended, from 2027-10-31 00:00 UTC | no (M4) | — | no (M4) | Enterprise (M6) | no (M4) |

A sixth state, `open`, is the standing of a build without a vendor key, which refuses nothing (§19).
`since` is the named licence's `starts` at 00:00 UTC (None in evaluation). `Standing.refusal(feature)`:
**`run`** (creation, compare, a soak's next cycle, a resume from a licence pause; the last two judged as of
when the cycle falls due, §8): `expired` M4, then `sso_fenced` M5; **`signoff`** (request, approval step): not an Enterprise licence (evaluation included),
M6; **`packs`**: `expired` M4, then not Enterprise M7. `sso_fenced` holds when the installation runs SSO
(D4) and no Enterprise licence is named, evaluation included. **`clock_behind`**, the
newest run's `created_at` minus `now` when over 5 minutes, refuses nothing: it is warned and recorded on
each run made while it holds.

## 7. Pairs

A **pair** is a `ds.specs.name`: all versions once, a graph spec once. A run is **in the window** from
`since` unless it finished before it (a run finishes after it is created, both by the database's
`now()`); evaluation's window is every run ever. The runs are aggregated first, on `runs_window_idx`
(§13); on PostgreSQL 16 it took 35 ms over 250,000 runs in the window and 4 ms over 20,000:

```sql
-- repo.counted_pairs(conn, since): each name with a run in the window from since, by its first such run
SELECT s.name, min(w.first_at) AS first_at
FROM (SELECT spec_id, min(created_at) AS first_at FROM ds.runs
      WHERE coalesce(finished_at, 'infinity') >= coalesce(%(since)s::timestamptz, '-infinity')
      GROUP BY spec_id) w
JOIN ds.specs s USING (spec_id)
GROUP BY s.name ORDER BY first_at, s.name
```

`repo.pair_targets(conn, names)` adds each name's `sides.a.target vs sides.b.target`, for display only
(`GET /v1/licence`, `ds licence`, the true-up). **Seats:** the first `limit` counted names (2 in
evaluation); unlimited passes uncounted. **Admission**, `licence.admit(ctx, conn, spec, *, lock=False) ->
(record, pair | None)`, shaped like `approvals.gate`: `need(ctx, conn, "run")` (M4, M5); a seated name
passes; a counted unseated one gets M3; a new name passes while fewer than `limit` names are counted
(returning its `licence.pair` row, `(object, {since, name, count, limit, targets})`, to which the caller
adds `run_id`: only the standing `admit` read holds `since`, `limit` and evaluation's installation id),
else M1, or M2 in evaluation. With `lock`, a new name first takes
`pg_advisory_xact_lock(0x44534C43)` ("DSLC", `repo.lock_pairs`, beside `_BLOB_LOCK`) and recounts.

**Grandfathering.** A seat is kept for its window. Each licence named counts from its own `since`, and a name with a run live at `since`
has an older first run, so it is seated first. After a trial inside a year, or at a downgrade, the smaller
licence seats its `pairs` earliest names and no more. An unseated name's cycle under way ends (a batch or
one-shot completes) and its soak pauses at the next boundary (M3) until a licence seats it; that pause's
resume is refused until then. Other resumes and keys in flight never recount. **Concurrency:** the unlocked
call runs before keys are resolved; the locked one in the insert transaction after
`approvals.gate(lock=True)` (approval `FOR SHARE`, DSLC, inserts). Under READ COMMITTED the recount sees
every committed run, so two new names at the limit give one run and one M1. Installs take DSLC before the
audit lock, as admissions do; revokes, resumes, boundaries and packs never take it: no deadlock.

| Entry | Path | Needs |
|---|---|---|
| Batch, soak, table scan, graph parity, file mode (`POST /v1/runs`, `ds run`) | `create_run` | a seated name, or a new one (counted) |
| One-shot (`POST /v1/compare`, its `run_id` form too; `ds compare`; the chat agent's `compare_key`) | `compare_one` | the same |
| An MCP caller's `compare_key` ([MCP.md](MCP.md) §7) | a check of `licence.standing`, then `compare_one` | a seated name only (or unlimited pairs): a new name is refused before the runner (T18) and never counted |
| A soak's next cycle (rolling re-resolve, rescan); a resume from `LICENCE_PAUSE` | `finish_cycle`; `resume_run` | the run allowed (M4, M5) and its name seated (M3) by the licence named when the cycle falls due; never counted |
| Other resumes; re-runs and new versions of a seated name; recheck passes | — | nothing more |
| A schedule's firing ([SCHEDULES.md](SCHEDULES.md) §7) | `create_run`, as `system:worker` | the same as a batch or soak: a seated name, or a new one, counted with a `licence.pair` row by `system:worker` |

A hash sample's run is one pair, its spec name ([SAMPLING.md](SAMPLING.md) §15): its frame's targets add
none, and every draw after the first (a rolling soak's next cycle, a resume after a refused draw, a new
run) is free once the name is seated. A second spec name over the same population is a second pair, which
shares the first's ledger (SAMPLING §9). A sampled soak paused at a boundary for the licence draws that
cycle's sample after its resume.

## 8. Enforcement points

| Where | Check | Refusal | API / CLI |
|---|---|---|---|
| `runner/runs.create_run`, after `approvals.gate`, before `tables.plan`; again after `gate(lock=True)` | `admit`, then `admit(lock=True)`, whose record is `params["licence"]` | M1–M5 | 409 `POST /v1/runs`; `ds run` exit 2 |
| `compare_one`, the same | the same | M1–M5 | 409 `POST /v1/compare`; `ds compare` exit 2; `ToolError` |
| `resume_run` when `stop_reason` is `LICENCE_PAUSE`, after `gate(lock=True)` | `need(ctx, conn, "run", name=…, prefix=…, at=cycle.due(spec, run, run["cycle"]))`: as of when its cycle falls due, or now if later | `run R-… cannot resume: ` and M3, M4 or M5, after `its cycle falls due on <UTC time>, when ` if that is later than now | 409 `POST /v1/runs/{id}/resume` |
| `runner/cycle.finish_cycle` when `going_on`, after `_rolled`, beside `attach` | `licence.stopped(ctx, run, spec, next_at)`, judged as of `next_at` (now if later), as `StopConditions.at_boundary` judges `until`: `standing(ctx, conn, at=next_at)` names the licence of that day and counts its window's pairs; a refusal starts the next cycle, rolled or rescanned as usual (so its census holds), paused with `LICENCE_PAUSE = "run_paused_licence"` | M3–M5 as data in the `paused` row (§11), the message logged | Run screen, `ds runs` |
| `api/routes/approvals.request_signoff` after `_sso`, first in its transaction; `_approve` for a sign-off, after the approver's own refusals | `need(ctx, conn, "signoff")` | M6 | 409 |
| `reports/pack._judged` after `refuse_purged` (so `problems` and `issue` agree) | `licence.pack_problems(standing, run)`, joined | M4, M7, M8 | 409, after the SSO 409, 503 and 403 |
| `runner/worker._Worker._sweep_once`, hourly (`watched_at`, like `purged_at`) | `licence.watch(ctx)`, which never raises | — | `licence.state` |

Refusals are 409 (`api/app._HANDLERS[LicenceRefused] = _conflict`, as `ApprovalRequired`), exit 2
(`cli/main._EXPECTED`) and a `ToolError` (`agent/tools._compare_key`). `LicenceRefused(Exception)` is
neither `ValueError` nor `LookupError`, so `routes/runs._runner_errors` never makes it 422 or 404. **At the
boundary** `paused = stopped is not None or (attach and attached is None)`, the licence reason wins,
`set_run_approval` runs whenever an approval was attached, and `paused` is audited by `system:worker`.
`resume_run` adds `LICENCE_PAUSE` to `cleared` and to `unread` (the pause precedes any key read of its
cycle, so a first approval attached on resume covers the cycle). A run paused for anything else (its
worker's `AUTH_PAUSE`, `KEY_PAUSE`, a revoke's `APPROVAL_PAUSE`, an operator) resumes whatever the licence,
ends its cycle and is judged at the next boundary. A cycle that falls due after the grace, or under a
smaller licence that starts before it, starts paused, and a renewal (or a licence seating the name)
covering that day lets it resume. `stopped` catches every error but
`psycopg.OperationalError`, logs `licence check at the boundary of run R-… failed (<type>): the soak goes
on` and returns None: the sweep would otherwise `fail_run` a live soak. A run paused for the licence past
its retention days is abandoned like an approval pause (RETENTION §4.3); a hold keeps its cycles, not the
run.

**A schedule's firing** ([SCHEDULES.md](SCHEDULES.md) §5, §7) meets the first row's checks as
`system:worker`. M1 to M5 start no run: the worker records the time as `schedule.missed {reason:
licence}` with code `expired`, `sso` or `pairs`, read from `licence.standing` (`state`, `sso_fenced`)
and never from the message, which is neither stored nor sent. In `ending` and `grace` firings run,
labelled by `licence.run_label` as any run. After the grace every due time is missed: its first as one
row, the later ones as one counted row a day, until a renewal, whose next due time runs with no
backlog (D9 concerns paused runs; a firing is a new run). A scheduled soak paused at a boundary for the
licence is still live, so the schedule's later times are missed as `overlap` until it is resumed or
cancelled.

**Code:** a new `deltasleuth/licence.py` (about 890 lines, many of them §9's and §10's binding messages; "a
contractual control") holds the functions
named here; `store/repo.py` gains `licence_facts`, `get_licence`, `insert_licence`, `withdraw_licence`,
`counted_pairs`, `pair_targets`, `lock_pairs` and `last_audit`. No dependency, no setting.

## 9. Messages (binding; each asserted)

**Refusals** (`LicenceRefused`; names past ten end `… and 3 more`):

- **M1** `spec cards would be pair 6 of 5 under licence LI-… (pro, 2026-10-01 to 2027-09-30): its seats since 2026-10-01 are accounts, customers, loans, payments, transfers, whose runs go ahead; a new pair needs a licence with more pairs (docs/mvp/LICENCE.md §7)`
- **M2** `spec cards would be a third pair: an unlicensed evaluation runs 2 pairs (customer-profile, linker); install a licence for installation I-… (docs/mvp/LICENCE.md §7)`
- **M3** `spec cards is not one of the 2 pairs an unlicensed evaluation runs (customer-profile, linker): its cycles under way end, and a new run or cycle needs a licence for installation I-… (docs/mvp/LICENCE.md §7)`; under a licence `spec cards is not one of the 5 pairs licence LI-… (pro, 2026-10-01 to 2027-09-30) seats since 2026-10-01 (accounts, customers, loans, payments, transfers): its cycles under way end, and a new run or cycle needs a licence with more pairs (docs/mvp/LICENCE.md §7)`
- **M4** `licence LI-… ended on 2027-09-30 and its 30 days of grace on 2027-10-30: new runs, compares, soak cycles and evidence packs are refused until a renewed licence is installed (ds licence install FILE); reads, reports, exports, holds, erasure, purge, audit, pack downloads and sign-offs under an Enterprise licence keep working (docs/mvp/LICENCE.md §6)`
- **M5** `SSO (auth.oidc) is an Enterprise feature and licence LI-… is pro: under SSO this installation keeps serving sign-in, reads, reports, exports, pack downloads and administration, and new runs, compares and soak cycles need an Enterprise licence (ds licence install FILE) or tokens mode (docs/mvp/LICENCE.md §6)`; `… and this installation is an unlicensed evaluation: …` in evaluation; with packs configured (this process's `packs`, or a `pack_key` in the newest `system.start`) the tail is `… need an Enterprise licence (ds licence install FILE); keep SSO, which evidence packs need (docs/mvp/LICENCE.md §6)`; plus ` (this installation's server last started in oidc mode)` when this process is not in oidc mode
- **M6** `four-eyes sign-off is an Enterprise feature and licence LI-… is pro (docs/mvp/LICENCE.md §6)`; in evaluation `four-eyes sign-off is an Enterprise feature and this installation is an unlicensed evaluation: install an Enterprise licence for installation I-…`
- **M7** `evidence packs are an Enterprise feature and licence LI-… is pro`; in evaluation `no evidence pack is issued in an unlicensed evaluation: install an Enterprise licence for installation I-…`
- **M8** `run R-… was made in an unlicensed evaluation: no evidence pack is issued for it; start a new run under the licence`; in an open build it ends `…; start a new run` (§19)

**Install and withdraw** (`LicenceError`: `error:`, exit 2, nothing written):

- **I1** (as amended, §19) `this build embeds no vendor key: it needs no licence and can verify none (docs/mvp/LICENCE.md §19)`
- **I2** `<file> is not a DeltaSleuth licence (deltasleuth-licence/1): line 4: expected 'order: …'`; a refused value names its field (`…: ends is more than 365 days after starts`, `…: customer begins or ends with a blank`); `…: over 4096 bytes`; `…: not UTF-8 text`
- **I3** `licence format 'deltasleuth-licence/2' is newer than this build reads: upgrade DeltaSleuth`
- **I4** `licence LI-… is signed by key 34e7a9ce0b6b…, which this build does not trust: upgrade DeltaSleuth, or ask for a licence signed by a current key`
- **I5** `licence LI-… does not verify with its key 34e7a9ce0b6b…: the file was changed after it was signed; install the file as received`
- **I6** `licence LI-… is for installation I-aaaa…, not this one (I-bbbb…): ask for a licence for I-bbbb… (ds licence id)`
- **I7** `licence LI-… ended on 2027-09-30 and its grace on 2027-10-30: install a current licence`
- **I8** `licence LI-… is installed already with other content: ask your DeltaSleuth contact which file is genuine`
- **I9** `licence LI-… was withdrawn on 2026-10-02: it is not installed again; ask for a new licence`
- **W1** `licence LI-… started on 2026-10-01: a started licence is replaced by a new one, not withdrawn (docs/mvp/LICENCE.md §5)`
- **Withdraw, also:** `licence LI-… is not installed`; `licence LI-… is withdrawn already`; `--reason must be 1 to 200 characters without control characters`; `DS_OPERATOR`: AUTH §4.2's.

## 10. Labels and warnings

**A run's record**, `params["licence"]`, written only at creation: `{licence_id, tier, state, ends}` (null
but `state: "evaluation"` in evaluation), plus `clock_behind: true`; never the customer. `approvals.plan`
drops only `approval`, so it is in `facts/plan.json` and `plan_sha256`. **`licence.run_label(run)`:**
`Unlicensed evaluation: run R-… was made without a licence. Not for production use; no evidence pack is
issued for it.`; `Run R-… was made in the 30-day grace of licence LI-…, which ended on 2027-09-30.`; `Run
R-… was made under licence LI-… (enterprise).`; each plus ` The database clock was behind the newest run
when it was made.` when recorded; none without a record.

**`Standing.warnings()`**, one line each: `Unlicensed evaluation of installation I-…: at most 2 pairs (1
used), no runs under SSO, no sign-off or evidence packs; not licensed for production use (ds licence)`;
`licence LI-… applies from 2026-10-01`; `licence LI-… ends on 2027-09-30 (in 12 days): install its renewal
(ds licence install FILE); new runs and evidence packs stop on 2027-10-31` (a lesser successor adds `; LI-…
(pro, 5 pairs) follows from 2027-10-01`; `in 1 day` the day before, `today` on the day); `applies from` lists
each licence that starts later but a like-for-like renewal, which is silent, and the successor the ending
line names; `licence LI-… ended on 2027-09-30: everything works until
2027-10-30, then new runs and evidence packs stop; install its renewal (ds licence install FILE)`; M4; M5;
`licence LI-… allows 5 pairs and 5 have runs since 2026-10-01: a new pair is refused` (over: `… 7 have runs
…: cards and loans get no new run or cycle`); `the database clock is 3 day(s) behind the newest run
(2027-10-02T08:00Z): licence dates are judged by a clock that went back`; `licence LI-… in the store does
not count: <why>` (but for a row signed by a removed key, §4); `licence LI-… was installed (audit row 812)
but is missing from the store: install it again (ds licence install FILE)`; and, from `licence.shown`,
`licence status unavailable (<type>): see the server log`.

| Surface | What it shows |
|---|---|
| UI, CLI, API | each warning as a `.banner` on every screen, linking to the Licence screen; `serve`, `worker`, `run`, `compare` print `warning: <line>` on stderr; `ds runs` and the Run screen show each run's label (and `Paused for the licence at the end of cycle 4`); `GET /v1/runs/{id}` and each `GET /v1/runs` item gain `licence` (record and `label`, or null); report downloads (`Content-Disposition`), the Run screen's saves (`licensing.reportFile`) and `ds report` files are named `R-…-<name>`, an evaluation run's `R-…-unlicensed-evaluation-<name>` (`licence.file_name`), their content unchanged; the P0 summary keeps `report.md` and `report.html`, its warning labelled |
| `report.pdf`, P0 summary | `reports/data.licence_line` after `pdf._title_block`'s approval lines; an evaluation or grace label joins `summary_document`'s `warning` |
| Evidence pack | `manifest.json` gains `licence: {licence_id, customer, order, installation_id, tier, pairs, starts, ends, state, key}` (`pairs` null when unlimited), `_FIXED` gaining its `customer`, `order`, `tier`, `state`; `rawvalues._GENERATED` gains `LI-` and 26 characters, and `I` among the 16-hex letters. The certificate's claim adds `Licence LI-… of Example Bank plc (order ORD-2026-014): enterprise, 2026-10-01 to 2027-09-30, installation I-…` (`; issued in its grace period`). `VERIFY.txt` and `ds packs verify` read named members: unchanged |
| Logs | `deltasleuth.licence`: WARNING at start, per state change, per uncounted or missing row, per failed boundary check; INFO per refusal, per boundary pause (`run R-… pauses at its boundary: <M3, M4 or M5>`, after `its cycle falls due on <UTC time>, when ` if that is later than now) and once per row signed by a removed key |

## 11. Audit actions (added to AUTH §6 and RETENTION §8)

| Action | Object | Detail | By |
|---|---|---|---|
| `installation.created` | `I-…` | `{installation_id}` | `system:migration` |
| `licence.install` | `LI-…` | `{sha256, customer, order, installation_id, tier, pairs, starts, ends, issued, key, applies: "now" \| "from <day>" \| "no: LI-… applies"}` | the CLI operator |
| `licence.withdraw` | `LI-…` | `{reason}` | the CLI operator |
| `licence.pair` | `LI-…` (`I-…` in evaluation) | `{since, name, run_id, count, limit, targets}` after `create_run` or `compare_one` for a new pair (`since` the licence's `starts`, null in evaluation) | the run's creator |
| `licence.state` | `I-…` | `{from, to, licence_id, ends}` when `(state, licence_id)` differs from the newest such row (`from` null without one): the SIEM's alert 30 days ahead, at the end, after grace | `system:worker` |
| `create_run`, `compare_one`; `paused` | run | gain `licence` (the record); `{reason: "run_paused_licence", approval_id, licence: {licence_id, tier, state, ends, refusal, …}}`: the record of the standing as of when the cycle falls due, and `refusal` `expired` (M4), `sso` (M5) or `unseated` (M3, adding `count`, `limit` and `since`: the pairs counted, the limit and the named licence's `starts`, null in evaluation); `Standing.paused` | as today; `system:worker` |
| `system.start`; `pack.issue` | `server`; `P-…` | gain `licence` (`{installation_id, state, licence_id, tier, pairs, ends}`, `pairs` the limit: 2 in evaluation, null when unlimited; null when `licence.shown` failed); `licence_id` | as today |

An MCP caller's `compare_key` writes no `licence.pair` row: it compares only a name that is seated
already ([MCP.md](MCP.md) §7, T18; a licence that starts between its check and the runner's `admit` could
still seat the name, and its row would say so). T18 names the licence as `Licence.named()` does (its id,
tier and dates), never the customer.

Customer names appear only in `licence.install` rows and packs. Refusals are logged, not audited, as
four-eyes refusals are. What the licence adds to a run's rows (`create_run`'s and `compare_one`'s record,
`paused`'s `licence`) holds no message, spec name or customer: every pack of the run carries them, and its
raw-value gate reads their strings (PACK §2.3), where a message's numbers (`30 days`, `§6`, `5 pairs`)
would be keys under an integer key pattern.

## 12. Interfaces

**API.** `GET /v1/licence` (viewer, `api/routes/meta.py`, unaudited; in AUTH §4.1 and `test_roles.ROUTES`);
no write route. Its body is `licence.status(ctx)`, which never raises:

```json
{"installation_id": "I-…", "state": "ending", "warnings": ["…"],
 "licence": {"licence_id", "customer", "order", "tier", "pairs", "starts", "ends", "issued", "key", "sha256"},
 "renewal": null, "allows": {"runs": true, "sso": true, "signoff": true, "packs": true},
 "pairs": {"limit": null, "since": "2026-10-01", "counted": [{"name", "first_at", "seated", "targets"}]},
 "clock_behind_seconds": null,
 "installed": [{"licence_id", "tier", "starts", "ends", "installed_by(_name)", "installed_at",
                "withdrawn_by(_name)", "withdrawn_at", "withdraw_reason", "in_force", "problem"}]}
```

`licence` and `renewal` are `Licence.fields()` but `installation_id` (`pairs` null when unlimited).
`allows`: `runs` when `refusal("run")` is None, `sso` (runs under SSO) under an Enterprise licence not
expired, `signoff` and `packs` when their refusals are None. `pairs.since` is the named licence's `starts`
day (null in evaluation). Each stored row is `installed`, oldest id first: its `tier`, `starts` and `ends`
null when it does not verify for this installation (`problem` says why, `Standing.uncounted`'s or, for a
row signed by a removed key, `Standing.retired`'s, §4), `in_force`
for the licence named unless expired. When the standing cannot be read the body is `state: "unknown"`, the
one warning `licence status unavailable (<type>): see the server log`, `installed: []` and the other members
null. In an open build the body is §19's: `state: "open"`, no warning, everything allowed, no pair counted.

**CLI** (`licence_app`, a Typer sub-app like `purge_app`):

| Command | Audit | `DS_OPERATOR` |
|---|---|---|
| `ds licence [--json]`; `ds licence id`; `ds licence show FILE\|-` (offline, no settings) | — | no |
| `ds licence install FILE\|-` (`-`: stdin) | `licence.install` | yes |
| `ds licence withdraw LI-… --reason TEXT` | `licence.withdraw` | yes |

`ds licence` prints `installation I-…`, `licence LI-…: <customer> (order <order>), <tier>, <pairs>, <starts>
to <ends>` (`<pairs>` as `5 pairs` or `unlimited pairs`; in evaluation `licence none: an unlicensed
evaluation`; in state `unknown`, after `installation -`, `licence unknown: the standing could not be read
(see the server log)`), `state <state>: <warning>` (the first warning; each other on its own line, indented two
spaces), `allows: runs yes, sso no, signoff no, packs no` and each counted pair as `pair <name>  first run
<time>  <targets>  seated` (or `no seat`); it exits 0 in every state. In an open build it prints `licence
none: this build needs no licence`, `state open`, every `allows` as `yes` and no pair, and `show` and
`install` exit 2 with I1 (§19). `--json` prints the API's body plus
`audit_head`: the true-up a renewal checks against the SIEM. `show` prints the body (the format line to
`key`) and `verifies with key 077646502f19… (trusted by this build)`, else exits 2 with I1–I5. A file is
read to at most 4097 bytes, and I2's `<file>` is its path as given, or `stdin` for `-`. `install` prints
`installed LI-… (enterprise, Example Bank plc, 2026-10-01 to 2027-09-30, unlimited pairs): in force now`
(or `in force from 2026-10-01; LI-… applies until then`, `…; an unlicensed evaluation until then`, or, for a
started licence a later-starting one outranks, `not in force; LI-… applies`), or `licence LI-… is installed
already`. When it applies now: `changes now: tier pro -> enterprise; pairs 5 -> unlimited; state grace ->
valid` (the items that changed; an evaluation's tier is `evaluation`), and a narrowing adds `cards, loans
have no seat: no new run, and their soaks pause at their next cycle`; a non-Enterprise licence on an SSO
installation adds `runs under SSO stay refused (M5)`, and, when it applies now and runs are allowed, `2
run(s) paused for the licence: R-…, R-…; resume each on its Run screen` (those `resume_run` lets resume, judged as it judges them: `runs.resumable`).
`withdraw` prints `withdrew licence LI-…: it is not installed again`. On the host: `docker compose run --rm
-T -e DS_OPERATOR="$(id -un)" app ds licence install - < deploy/licence/LI-….txt`.

**UI.** `api.ts` gains `licence()`, loaded by `main.tsx` after `me` (no banner if it fails). The server
decides; with the first warning as the reason (`The licence does not allow <feature>: see the Licence
screen.` when there is none), the UI disables Runs' "Start" and Key's "Compare now" without
`allows.runs`, Run's "resume" of a licence-paused run without `allows.runs`, "Request sign-off" without
`allows.signoff`, "Issue evidence pack" without `allows.packs`, and nothing while `allows` is null
(unknown). A new `Licence.tsx` at `#/licence`, open to every role and linked from the nav and each banner
(Admin is hidden from tokens, Community's and Pro's mode): the id, licence (`No licence: an unlicensed
evaluation.`, or `Licence unknown: the standing could not be read (see the server log).` in state
`unknown`), renewal, state, `allows`, counted pairs with their seats, and who installed or withdrew each
licence and why. The Run screen saves a report as `R-…-<name>` (`R-…-unlicensed-evaluation-<name>` for a
run made in evaluation), as the download's `Content-Disposition` names it. The Licence screen's words, the
run's pause line, the report names and the disabled buttons' reasons are `web/src/licensing.ts`'s (not
`licence.ts`, which a case-insensitive file system cannot tell from `Licence.tsx`). In an open build the
screen is §19's: no licence is needed, no banner shows and no control is disabled.

## 13. Migration `010_licences.sql`

```sql
-- Licence keys and entitlements (docs/mvp/LICENCE.md §5): this control-plane database's installation id,
-- made once and kept by a dump and restore, and the licences installed on it, verified again at every
-- read against the vendor keys the build embeds: a row written with SQL grants nothing. The id comes from
-- the first audit chain row, the same in a dump from before 010, so 010 applied to that dump again makes
-- the same id; it is random only on a store with no audit row yet (a fresh one).
CREATE TABLE ds.installation (
    installation_id text PRIMARY KEY CHECK (installation_id ~ '^I-[0-9a-f]{16}$'),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX installation_one ON ds.installation ((true));  -- one row
INSERT INTO ds.installation (installation_id)
    VALUES ('I-' || left(encode(sha256(convert_to(coalesce(
        'deltasleuth-installation' || E'\n' || (SELECT hash FROM ds.audit_chain ORDER BY seq LIMIT 1),
        gen_random_uuid()::text), 'UTF8')), 'hex'), 16));
CREATE TRIGGER installation_append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON ds.installation
    FOR EACH STATEMENT EXECUTE FUNCTION ds.append_only();
CREATE TABLE ds.licences (
    licence_id      text PRIMARY KEY CHECK (licence_id ~ '^LI-[0-9A-Z]{26}$'),
    text            text NOT NULL CHECK (octet_length(text) <= 4096),  -- the normalised file (§3)
    sha256          text NOT NULL UNIQUE CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    installed_by    text NOT NULL,
    installed_at    timestamptz NOT NULL DEFAULT now(),
    withdrawn_by    text,
    withdrawn_at    timestamptz,
    withdraw_reason ds.note,
    CHECK ((withdrawn_by IS NULL) = (withdrawn_at IS NULL) AND (withdrawn_at IS NULL) = (withdraw_reason IS NULL))
);
-- A withdrawal is written once, as of the transaction's now(): a backdated one would re-date a licence.
CREATE FUNCTION ds.tg_licences() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_withdrawal text[] := ARRAY['withdrawn_by', 'withdrawn_at', 'withdraw_reason'];
BEGIN
    IF TG_OP = 'DELETE' OR OLD.withdrawn_at IS NOT NULL OR NEW.withdrawn_at IS DISTINCT FROM now()
       OR (to_jsonb(NEW) - v_withdrawal) IS DISTINCT FROM (to_jsonb(OLD) - v_withdrawal) THEN
        RAISE EXCEPTION 'ds.licences: licence % is only ever withdrawn, once, as of now()', OLD.licence_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER licences_withdraw_only BEFORE UPDATE OR DELETE ON ds.licences
    FOR EACH ROW EXECUTE FUNCTION ds.tg_licences();
CREATE TRIGGER licences_no_truncate BEFORE TRUNCATE ON ds.licences
    FOR EACH STATEMENT EXECUTE FUNCTION ds.append_only();
-- The runs in a licence window, those not finished before it starts (repo.counted_pairs, §7).
CREATE INDEX runs_window_idx ON ds.runs ((coalesce(finished_at, 'infinity')), spec_id, created_at);
```

Ran on a local `postgres:16-alpine` (`--pull never`) through `store/db.migrate` over 001–009: a second
installation row, UPDATE, DELETE or TRUNCATE of `ds.installation`, an edited text, a partial, second or
backdated withdrawal, DELETE or TRUNCATE of `ds.licences` were refused; `migrate` with only 001–009's files
then applied nothing and raised nothing on the store at 010 (an older release beside it); a `CREATE
DATABASE … TEMPLATE` copy kept the id and the licence (a `pg_dump`, whose data section carries the row
before the post-data triggers, which refuse no INSERT, was not run: the test host has none); a copy taken
at 009, migrated after its source had applied 010 and installed a licence, got the source's id, and the
same licence file installed on it, in force now; an empty store's id is random. `store/db.py` gains
`LICENCES = 10`; `migrate` audits `installation.created` before `system.migrate`.

## 14. The vendor tool (`dsv`, outside the shipped product)

`tools/dsv.py` in this repository, outside the `deltasleuth` package: no entry point, and the image
copies only `deltasleuth/` (Dockerfile), so no installation runs it. It holds no secret: a licence is only
as strong as the vendor's private key, which never enters the repository. About 170 lines of Typer
importing `deltasleuth.licence` (`body`, `new_id`, `parse`, `VENDOR_KEYS`), run with `uv run python
tools/dsv.py` on the maintainer's offline machine. Keys come only through `file:`
references (`read_secret_bytes`: absolute paths) to files the maintainer writes, a passphrase through
`--passphrase file:PATH` on separate media or a `getpass` prompt, never an argument value or the
environment.

- `dsv keygen --out DIR [--passphrase file:…]`: `vendor-<fp16>.key.pem` (P-256, PKCS#8,
  `BestAvailableEncryption`, mode 0600) and `.pub.pem`, never over a file; prints the fingerprint, the
  `SHIPPED_KEYS` entry (double-quoted bytes lines, as `ruff format` writes them) and the `vendor-keys.txt`
  line (§4); refuses a `DIR` in a git working tree before writing anything; a prompt asks twice.
- `dsv issue --key file:… --customer T --order T --installation I-… --tier T [--pairs N|unlimited]
  --starts D --ends D --out DIR --register FILE`: refuses what `parse` would (a customer or order that
  begins or ends with a blank too), signs `licence.body(...)` deterministically, re-parses it against the
  checkout's `VENDOR_KEYS` (`key … is not in VENDOR_KEYS: no build trusts it yet`), appends `{licence_id,
  sha256, customer, order, installation_id, tier, pairs, starts, ends, issued, key}` to the register of
  genuine licences and then writes `LI-….txt`, so no genuine licence goes unregistered. Pairs default to 2, 5,
  unlimited by tier; `--passphrase file:…` or a prompt, as keygen.
- `dsv register --register FILE [--ending DAYS]`: what was issued, or ends within DAYS (renewals go out 60
  days ahead).

## 15. Upgrade and operations

**Upgrade**, once keys are released, with one restart as downtime:

1. Take the new release while the old one serves: verify its signed bundle on the Docker host, which
   loads its image, and extract its kit (DEPLOY §2, [RELEASE.md](RELEASE.md)); the running container is
   left alone. A source build runs `git pull && docker compose build`.
2. `docker compose run --rm app ds licence id`: the new image applies 010 and prints the id. The old
   release keeps serving, restarts included: its `migrate` applies only the files it ships (§13). A
   backup from before this step, restored later, gets the same id when 010 applies to it (§5); only a
   store whose audit log was empty gets a random one, which a backup taken after this step keeps.
3. The customer sends the id; the vendor issues the licence (§14).
4. `ds licence install -` as in §12, then `docker compose up -d` (the new release).

A run without `params["licence"]` (before 010, or made by the old release after it) has no label and is
packed under the licence at issue. A site that embeds and signs with its own keys is its own vendor.

**Operations** (DEPLOY, RUNBOOK): alert on `licence.state`; renewals arrive at T−60 and, installed early,
silence the warnings; before a grace ends, finish sign-offs (each holds its run) or `ds purge hold` what
awaits one; after it, install the renewal and resume the runs paused for the licence. A fresh database is
a fresh evaluation: DEPLOY §6's smoke tests run after the licence is installed, and under Community or Pro
each smoke-test spec run in the period is one of its pairs, so run them on a scratch database (another
DSN) instead. Switching an SSO installation to tokens mode means removing `packs` (`load_settings` refuses
it), after which the revocation list of earlier packs is no longer signed. A database rebuilt without a
backup needs a reissue.

## 16. Tests

No image is pulled (local `postgres:16-alpine`, AUTH's fake IdP). The clock a test sets is `judge`'s `now`
(pure: `test_judge_edges`, the warnings); integration tests date licences from the database's `now()`
(`starts=today − 340` for `ending`) and store grace or ended ones with `repo.insert_licence`; `clock_behind`
moves a run's `created_at` ahead with SQL. CI sets `DS_REQUIRE_TOOLS=1`. Every §9 message is asserted.

| File | Tests |
|---|---|
| `tests/unit/test_licence.py` | `test_parse_example`; `test_crlf_bom_blanks_verify`; `test_grammar_I2` (line missing, moved or extra; bad id; control character; a customer or order with a leading or trailing blank; tier; `pairs` 0, `5.0`, 10000; `ends < starts`; 366 days; a no-break space ending the signature line; 4097 bytes); `test_no_key_I1`; `test_newer_format_I3`; `test_untrusted_key_I4`; `test_flipped_byte_and_borrowed_fingerprint_I5`; `test_openssl_verifies` (§3's recipe on a mangled file); `test_shipped_keys_match_fixture` (and `REMOVED_KEYS`); `test_judge_edges` (`ends−31`, `−30`, `ends`, `+1`, `+30`, `+31`; a future licence alone); `test_what_a_server_start_records` (`detail()`: the evaluation's two pairs, a limit, null when unlimited); `test_renewal_silences_ending`; `test_later_starts_wins_ties_by_issued`; `test_withdrawal_counts_only_before_starts`; `test_foreign_or_edited_rows_grant_nothing`; `test_unreadable_rows_grant_nothing`; `test_rows_of_a_removed_key_are_not_warned`; `test_a_removed_key_row_logged_once_not_warned`; `test_missing_installed_row_warned`; `test_sso_fence_by_last_start`; `test_evaluation_fences_like_community`; `test_clock_behind_refuses_nothing`; `test_labels_warnings_golden` |
| `tests/unit/store/test_migration_010.py` | `test_010_over_009_and_empty` (over 009 the id derived from the first chain row, an empty store's random); `test_installation_one_row_append_only`; `test_withdrawn_once_as_of_now` (a backdated SQL withdrawal refused); `test_installation_created_audited`; `test_template_copy_keeps_id_and_licence`; `test_backup_before_010_keeps_id` (a copy taken at 009, migrated after its source was licensed: the same id, the licence installs); `test_store_at_010_opens_with_001_to_009_files` |
| `tests/unit/runner/test_licence_admission.py` | `test_evaluation_two_then_M2`; `test_evaluation_third_name_M3_soak_pauses_at_boundary`; `test_versions_reruns_cycles_and_one_shots_of_a_counted_name_free`; `test_one_shot_counts_a_new_pair`; `test_graph_and_table_count_once`; `test_pro_sixth_M1`; `test_trial_then_pro_seats_first_five`; `test_live_soaks_carried_into_a_smaller_window`; `test_two_new_names_race` (the second waits on `Lock` in `pg_stat_activity`, then M1); `test_refused_before_resolve`; `test_expired_M4`; `test_batch_in_flight_completes_past_grace`; `test_auth_paused_batch_resumes_and_completes_after_grace`; `test_licence_paused_soak_resume_refused`; `test_boundary_pause_then_resume_after_install`; `test_rolling_soak_paused_cycle_keeps_census`; `test_boundary_error_never_fails_soak`; `test_cycle_due_after_the_grace_starts_paused` (a 30-day `cycleInterval` crossing the end of the grace; the resume refused until a renewal); `test_downgrade_before_the_cycle_falls_due_pauses_it`; `test_a_row_that_cannot_be_read_grants_nothing`; `test_sso_fence_M5_suffix`; `test_M5_advice_never_a_settings_error` (with `packs`, no tokens-mode advice; without, the config minus `auth.oidc` loads); `test_compare_key_tool_error`; `test_licence_pair_once`; `test_nothing_locks_out` (expired, and an evaluation under oidc: a worker's pause resumed, pause, hold, release, cancel, a batch in flight, a purge pass) |
| `tests/unit/api/test_licence_api.py` | `test_get_licence_states_roles` (seats); `test_refusals_409` (runs, compare, a licence-paused resume, sign-off request and step, pack); `test_packs_M4_M7_M8`; `test_enterprise_pack_carries_licence` (manifest with `pairs: null`, certificate, the raw-value gate under a lowercase-word key pattern); `test_signoff_after_grace_holds`; `test_runs_list_and_file_names_labelled`; `test_nothing_locks_out` over evaluation under oidc, Pro under oidc with `packs`, expired Enterprise and Pro, withdrawn, an edited or deleted row and `standing` raising: fake-IdP sign-in, every GET in `test_roles.ROUTES`, reject, revoke, pause, cancel, a non-licence resume, user disable, pack download and revocations; `test_get_licence_unknown` |
| `tests/unit/cli/test_cli.py` | `test_licence_id_stdout`; `test_show_without_settings` (a mailed file, and stdin: I2 names `stdin`); `test_install_refusals_write_nothing` (I2, I4–I9); `test_install_twice`; `test_concurrent_installs_serialise`; `test_install_stdin`; `test_withdraw_before_starts`; `test_withdraw_started_W1`; `test_no_withdrawal_returns_to_evaluation`; `test_run_over_limit_exit_2` (no run written; the spec file stored and audited `spec.create`, as an upload); `test_warnings_only_four_commands` (and `licence none: an unlicensed evaluation`); `test_licence_unknown_not_evaluation`; `test_evidence_commands_when_expired` (`audit verify`, `packs verify`, `packs download`, `purge`, `purge release` of the sign-off's hold, `purge hold`, `report`); `test_report_file_names_labelled`; `test_install_needs_ds_operator` (and both writing commands in the `DS_OPERATOR` list) |
| others | `test_dsv.py` (keygen and its git-tree refusal, issue and its refusals, the parse round trip, register); `runner/test_worker.py` `test_watch_once_per_change`; `reports/test_licence_reports.py` `test_licence_labels`, `test_plan_record_without_customer`, `test_raw_value_gate_licence_texts`, `test_licence_pauses_hold_no_raw_value` and `test_a_soak_paused_for_the_licence_keeps_its_packs` (a real boundary pause, sign-off, renewal and resume under `^[0-9]+$` and `^[1-9][0-9]{0,5}$`: no pack problem); `test_release.py` `test_two_vendor_keys`, `test_no_private_key_tracked`; `web/test/licence.test.ts` (of `web/src/licensing.ts`, `reportFile` and `namedLine` included, and `api.licence()`) |

**The open build's (§19).** The suite runs as the open build (§4, Tests). The tests above that need a
vendor build ask for `vendor_keys`: `test_licence.py`, `runner/test_licence_admission.py`,
`api/test_licence_api.py` and `reports/test_licence_reports.py` whole (`pytestmark`), in `cli/test_cli.py`
each that installs, shows or withdraws a licence or meets the evaluation (`test_licence_id_stdout` and
`test_licence_unknown_not_evaluation` hold in both builds), `runner/test_worker.py`
`test_watch_once_per_change` and `store/test_migration_010.py`'s three that sign a licence
(`test_dsv.py` sets its own key). So do the tests elsewhere that hold an evaluation's label, limit or
fence: `api/test_prechecks_api.py`'s and `cli/test_cli.py`'s labelled pre-check, `api/test_samples.py`'s
D10, `runner/test_schedule_fire.py`'s two licence misses, `runner/test_readiness_watch.py`'s Community
census and `mcp_server/test_tools.py` `test_compare_key_seats_no_pair`. Every other test expects the open
build: null records in `create_run` and `compare_one` (`api/test_audit.py`, whose `system.start` test
holds both builds' detail), unlabelled
report names and P0 summary (`e2e/test_cli.py`), no label on a pre-check (`runner/test_precheck_run.py`,
`api/test_prechecks_api.py`) or in `ds runs` and `ds precheck` (`cli/test_cli.py`), and an oidc client,
harness or signed cycle without a licence (`licensed_once` installs none).

| File | Tests |
|---|---|
| `tests/unit/test_licence_open.py` | `test_no_vendor_key_is_the_open_standing` (rows, an oidc start, a pack key, a missing install and a clock behind change nothing); `test_a_vendor_key_brings_the_evaluation_back`; `test_a_file_is_neither_shown_nor_installed` (I1); `test_a_run_s_own_label_is_kept_and_new_runs_carry_none` (and M8 without `under the licence`); `test_every_name_is_admitted_and_none_counted` (ten names, with the lock too; `need` for sign-off and packs; then a vendor key seats the first two); `test_status_and_shown_say_nothing_of_a_licence` (a row a vendor build stored is listed with I1; nothing logged; `install` writes nothing); `test_watch_audits_the_open_state_once_and_closes_an_evaluation_s` |
| `tests/unit/runner/test_licence_open.py` | `test_a_third_and_a_tenth_name_run` (and an eleventh as a one-shot; no `licence.pair`; the worker's one `licence.state`); `test_runs_compares_and_soak_cycles_under_sso_need_no_licence`; `test_a_store_an_evaluation_left_goes_on_under_an_open_build` (a soak the older build paused for the licence resumes and completes, its name runs again, the evaluation's run keeps its record, `evaluation -> open` audited, and a vendor key refuses the name again); `test_a_range_of_licence_misses_an_evaluation_held_is_chained_once` (a schedule's second refused time, held by the older build: the open build's worker chains it at the next firing, which runs with a null record; the newest firing's text until then; the warning a `schedule.missed` rule sends) |
| `tests/unit/api/test_licence_open_api.py` | `test_get_licence_says_open` (both viewers, unaudited; `system.start`); `test_work_under_sso_needs_no_licence` (`test_refusals_409`'s six requests answer 202, 200, 200, 201, 200 and 201; the manifest's `licence` and `pack.issue`'s `licence_id` null; unlabelled runs and report names); `test_a_run_made_in_an_evaluation_keeps_its_label` |
| others | `cli/test_cli.py` `test_an_open_build_needs_no_licence` (three pairs, no licence line from `serve`, `worker`, `run`, `compare` or `runs`, `ds licence` and `--json`, `id`, `install` and `show` exit 2 with I1, nothing written); `reports/test_pack_contents.py` `test_a_pack_holds_its_layout_and_facts_bound_to_audited_hashes`, `mcp_server/test_tools.py` `test_each_result_holds_exactly_its_names` and `api/test_prechecks_api.py` `test_a_licensed_pre_check_carries_no_label`, each in both builds (`build`); `mcp_server/test_tools.py` `test_compare_key_asks_for_no_seat_in_an_open_build`; `core/test_notify_payloads.py` `test_an_open_build_s_state_asks_for_no_licence`; `test_release.py` `test_two_vendor_keys` (none, or two and more); `test_release_tool.py` `test_every_refusal_leaves_nothing` (`notes that do not say the build is open`, `a vendor build's notes that say it is open`, `a vendor build's notes say nothing of it`); `web/test/licence.test.ts` `an open build needs no licence: its screens say so, disable nothing and show no banner` (`namedLine`, `installLine`, `refusal`, the one banner per warning in `main.tsx`, the Licence screen without pairs, the badge, a plain report name) |

**Existing tests that change** (as amended, §19: outside the vendor tests named above they hold the
open build's values): `create_run`, `compare_one` and `system.start` detail, params and plan-hash
equalities, with a null `licence` (`system.start`'s detail is held in both builds, by `api/test_audit.py`
`test_the_server_start_records_the_role_mapping` over `build`, and under a licence by `test_licence.py`
`test_what_a_server_start_records`; a vendor build's `create_run` and `compare_one` records by
`runner/test_licence_admission.py`: `test_licence_pair_once`, and `test_one_shot_counts_a_new_pair` for
the one-shot's);
`test_roles.ROUTES`; `system.migrate` versions with 10 (`api/test_audit.py` `[5..10]` and
`[1..10]`, `evidence/test_rekey.py` `[1..10]` and `[7, 8, 9, 10]`, `store/test_migration_006.py` and
`test_migration_008.py` `range(1, 11)`) and audit-action lists with `installation.created`; report file
names and the P0 summary in `e2e/test_cli.py` (its runs are an open build's: no label, and no
`unlicensed-evaluation` in a file name; runs a test stores directly carry no record in either build, so
`api/test_runs.py`, `api/test_adjudications.py`, `api/test_packs.py` and the report.pdf tests keep their
names and title blocks); `licence: null` on each run in
`evidence/rekey_api.golden.json`; the manifest's `licence`, the certificate's line and `pack.issue`'s
`licence_id` in `reports/test_pack_contents.py`; `ds runs`' licence line and `ds --help` in
`cli/test_cli.py`; in a vendor build (`vendor_keys`) SSO tests install Enterprise before
their runs and sign-offs through `licensed_once`, which installs nothing in the open build:
`api/conftest.py` `open_client` and `runner/conftest.py` `open_harness` in oidc mode
(`tier="enterprise"`; `None` for none, which is a vendor build's evaluation), so
`packs_client`, `api/test_approvals.py`, `runner/test_approvals.py`, `runner/test_census.py` and
`runner/test_scan.py`; `reports/packs.py` `signed_cycle` (`tier`; `mode="soak"` leaves the boundary to
the test), so `signed`, recording the licence in the run's `params` and `create_run` row as the runner
does; the CLI's SSO tests and a pack test that
calls `create_run` directly. Oidc tests of what needs no licence and count every audit row (sign-in
failures, a chat session, audit reads) open their client with `tier=None` and run as the open build, so
with no licence either way; `api/test_roles.py` `test_a_chat_session_is_its_creator_s` runs in both
builds (`build`), in a vendor build as an evaluation under SSO, where a run would be refused (M5), and
`test_nothing_locks_out` holds sign-in and the audit reads there. In a vendor build a test storing a
third spec name in one database calls `licensed`.

## 17. Documents to amend

- **AUTH** §1 (licences gate work, never sign-in; "What each mode is for": oidc runs under Enterprise, tokens
  is Community's and Pro's production mode, D11), §4.1, §4.2, §4.3 (the licence after the gate in
  `create_run`, `compare_one`, a licence-paused `resume_run` and a soak's boundary; `compare_key`'s
  `ToolError`), §6 (§11), §9 (banners, `#/licence`).
- **APPROVALS** §1.2 (a sign-off also needs an Enterprise licence, M6), §4 (the licence pause; `cleared`,
  `unread`), §6.1–§6.2 (M6), §10.
- **PACK** §1.1 (M4, M7, M8), §2.2 (manifest, certificate), §2.3 (`LI-`, `I-`, `_FIXED`), §7.2, §7.4, §10
  (a run without a licence record is packed under the licence at issue).
- **RETENTION** §4.3 (`run_paused_licence`), §8; purge, holds and erasure never consult the licence.
- **CONTRACTS** §1, §2 (no setting), §3 (010), §4, §5, §6 (`compare_key` refused by the licence), §7, §8,
  §10 (test key, dated licences).
- **DEPLOY** §3 "Sign-in" (tokens: also Community's and Pro's production mode; oidc runs need Enterprise)
  and a new "The licence" (the id, `deploy/licence/` keeping the files, a fresh database
  a fresh evaluation); §4; §5
  "Acceptance checklist"; §6 (install the licence first; smoke-test specs are pairs, or use a scratch
  database); §9; §10 "Upgrading to licence keys" (§15), "Backups and restore", "The SIEM copy and host
  auditing"; §11 (M5 and tokens mode against `packs`).
- **RUNBOOK**, a new "Licence" section; "Four-eyes approvals and sign-off" and "Evidence packs" (Enterprise
  only, M6, M7); "Try it on the mock pairs" (two pairs per unlicensed database, a fresh database a fresh
  evaluation); "Security and privacy" (no phone-home); "Troubleshooting" (tokens mode means removing
  `packs`).
- **README**; **PLAN**; **architecture** D.1
  (tampering: contractual).

## 18. Honest limits, deferred, size

| Who | Can | How it shows |
|---|---|---|
| The operator | patch the checks; edit the tables with triggers disabled; write a sign-off's `ds.approvals` rows with SQL (M6 is the route's check, not `ds.tg_approvals`') | no genuine `licence.install` SIEM row; a deleted row warned from its audit row; the customer on every label; the register; no `approval.grant` SIEM line |
| Whoever keeps an older build | run a build from before licence keys, which ignores the licence | `system.start` without `licence`; runs without a record; the contract |
| Whoever copies the database | run a second environment on one licence | not detected: one installation id in two `system.start` streams; the contract |
| Whoever creates a fresh database | restart the evaluation | a new id, no history, no SSO runs, never a pack |
| The host administrator | set the clock back to extend a licence | `clock_behind` on banners, runs and audit rows |
| A leaked vendor key's holder | mint licences | removed by a release; older builds still trust it; the register |
| A customer | reuse one spec name for several comparisons; leave a mid-term downgrade or correction uninstalled, as it applies only once installed (offline) | `targets` in the true-up; the register against `ds licence --json` |

Community and Pro run in tokens mode (D11); days are UTC; DSLC queues new-pair creations and installs
behind a large key insert; the pair count reads the window's runs twice per new run (§7); a rolling soak
paused for the licence reads its key listing at the boundary. **Deferred:** a UI upload; per-feature flags
(format 2); a binding that marks clones; audited refusals; HSM custody of vendor keys; remote revocation
never (air-gapped). **Size** (as built): about 1,350 product lines (890 of them `licence.py`, many of those
binding messages), 200 TypeScript and 3,000 test lines, and the 170-line vendor tool; no new dependency.

## 19. The open build

**The rule (binding, O4).** A build that embeds no vendor key has no limits and needs no licence: unlimited
pairs, sign-off, evidence packs and SSO runs all work, and the licence code stays in place, switched off.
Under §1 to §18 alone no licence could be installed in such a build, so every installation would stay an
unlicensed evaluation of two pairs without sign-off, packs or runs under SSO.

**The seam.** Enforcement exists only in a build that trusts at least one vendor key. `licence.judge`
reads `VENDOR_KEYS` at each call; empty, it returns `Standing(installation_id, now, "open", None,
rows=…)` (`licence.OPEN`) and reads no stored row: nothing is verified, counted, refused, warned of or
recorded. A key in `VENDOR_KEYS`, a test's (`vendor_keys`) or a later vendor build's `SHIPPED_KEYS`,
makes the next decision §1 to §18's, the evaluation included, with no setting and no restart: that code
is unchanged but for the lines below, and its tests are kept (§16). No setting, environment variable
or file opens or closes a build: only the keys it embeds.

**The open standing, as built:**

| Of | In an open build |
|---|---|
| Pairs | `limit` is None, unlimited; `standing` returns before `repo.counted_pairs`, so `counted`, `names` and `seats` are empty; `admit` returns `(None, None)` for every name, under `lock` too, without the pair lock; no `licence.pair` row is written and nothing new is stored. A third and a tenth spec name run |
| Refusals | `refusal("run")`, `("signoff")` and `("packs")` are None (`enterprise` is true, so `sso_fenced` is false; an unknown feature is still a `ValueError`); `unseated` and `paused` are None. M1 to M7 are never raised: a run, a compare, a soak's next cycle (`stopped` is None: no boundary pauses for the licence), a resume from `LICENCE_PAUSE`, a sign-off's request and step and a pack's issue go ahead in every auth mode, oidc too; no schedule's time is missed for `licence`; an MCP caller's `compare_key` meets no T18 (the pairs are unlimited) |
| A run's record | `Standing.record()` is None: `params["licence"]` is stored as null, and `create_run`'s and `compare_one`'s `licence` is null. So `run_label` is None and `file_name` is `R-…-<name>`: no licence line in `ds runs`, on the Run screen, in `report.pdf`, the P0 summary, an MCP result or a notification, and `GET /v1/runs` items carry `licence: null` |
| Warnings | `warnings()` is `[]`: no banner, no `warning:` line from `serve`, `worker`, `run` or `compare`, nothing logged at start, and nothing logged or said of a stored row |
| `allows()` | `{runs: true, sso: true, signoff: true, packs: true}`: no control is disabled, and readiness never calls its `signoff` or `pack` gate `unavailable` for the licence (its reports' header prints `licence state open`) |
| `detail()` in `system.start` | `{installation_id, state: "open", licence_id: null, tier: null, pairs: null, ends: null}` |
| `GET /v1/licence` | `{installation_id, state: "open", warnings: [], licence: null, renewal: null, allows: (all true), pairs: {limit: null, since: null, counted: []}, clock_behind_seconds: null, installed: […]}`; a row a vendor build stored is listed with its people, `tier`, `starts` and `ends` null, `in_force: false` and `problem` I1 |
| `ds licence` | `installation I-…`, `licence none: this build needs no licence`, `state open`, `allows: runs yes, sso yes, signoff yes, packs yes`, no pair line, exit 0; `--json` is the body above plus `audit_head` |
| `ds licence id` | unchanged: the id alone, which a later vendor build's licence names |
| `ds licence install`, `show` | I1, `error: this build embeds no vendor key: it needs no licence and can verify none (docs/mvp/LICENCE.md §19)`, exit 2, before the file's grammar is read; nothing is written or audited. `withdraw` is unchanged (it verifies nothing) |
| `licence.watch` | as §11: one `licence.state` row `{from, to: "open", licence_id: null, ends: null}` when the newest such row is not `open` (`from` null on a fresh database, `evaluation` on one an older build ran), logged at INFO (`licence state evaluation -> open (None)`), never again while the build is open |
| Notifications | `core/notify.State` gains `open`, which a rule's `states` may name; that row's event is `info`, titled `no licence is needed`, its text `Installation I-…: no licence is needed (was evaluation); this build enforces none.`; `notify.daily` says `licence open` |
| Table pre-checks | `evaluation` is false: no label on an item, a screen, `ds precheck` or an export's name and head |
| Evidence packs | the manifest's `licence` is null, the certificate's claim has no licence line and `pack.issue`'s `licence_id` is null: the pack names no licence, having none. In a vendor build the member is never null (an evaluation gets no pack), so null says an open build issued it |
| The UI | `api.ts`'s `state` gains `open`, whose badge is not a warning's. The Licence screen prints the installation id, `This build needs no licence and limits nothing.` (`licensing.namedLine`), `Allows: runs yes, sso yes, signoff yes, packs yes` and, in place of how a licence is installed, `Any number of pairs, four-eyes sign-off, evidence packs and runs under SSO work without a licence. ds licence install refuses every file: this build embeds no vendor key to verify one with.` (`licensing.installLine`); it has no Pairs section, none being counted, and lists installed licences only when an earlier vendor build stored one, each with I1 under "Does not count". No screen shows a licence banner (one a warning, and there is none), no control is disabled (`refusal` is undefined for every feature), a run shows no licence line and its reports are saved as `R-…-<name>`; a run or pre-check an evaluation made keeps its label |

**What stays.** Every check that is not the licence's: roles, the four-eyes gate and sign-off's own rules
([APPROVALS.md](APPROVALS.md)), SSO for sign-off and `packs` needing `auth.oidc`, a pack's other refusals
([PACK.md](PACK.md) §1.1), each sign-in mode's rules ([AUTH.md](AUTH.md)), retention, the schema guard, a
sample's refusals. §2's invariants hold as before: nothing there asked the licence.

**History is not rewritten.** On a store that ran as an evaluation under an older build that enforced the
licence (each build from migration 010 to the open build, none of which embedded a vendor key, or a vendor
build):

- a run made then keeps its record, `state: "evaluation"`, written at its creation (§10): its label, its
  reports' `R-…-unlicensed-evaluation-<name>`, its P0 warning and its notifications' `[unlicensed
  evaluation]` stay, for a soak made then through its later cycles too; a pre-check made then keeps
  `evaluation: true`;
- such a run still gets no pack, M8 (D8: its label says that none is issued for it, and no certificate
  may contradict it), ending `…; start a new run`; a new run of the same spec carries no label and is
  packed;
- a soak paused for the licence resumes when a person resumes it (D9: nothing resumes by itself);
- `licence.pair`, `licence.install`, `licence.state` and `schedule.missed {reason: licence}` rows stay,
  and the worker's first `watch` adds `{from: "evaluation", to: "open"}`;
- a schedule whose times the older build refused for the licence may hold the later ones as a range not
  yet chained ([SCHEDULES.md](SCHEDULES.md) §5 step 7). The open build misses no time for a licence, and
  under it that range is chained once, as the older build left it and as step 7 chains any held range:
  one `schedule.missed {reason: licence}` row, by `system:worker` when it settles the schedule's next due
  time, which the licence no longer refuses, or at its first sweep once the range's first time is 24 h
  old, or by a person whose save, pause or delete of the schedule comes first; dropping it would lose
  those times from the chain. A `schedule.missed` rule sends that row as any other, a warning (`… did
  not start a run for 1 more time since …, for the same reason: the licence refused the run (pairs).`).
  Until its next due time is settled, the schedule's newest firing still reads as the older build left
  it on the Schedules screen and in `ds schedules`: `not started: the licence refused the run (pairs):
  see Licence`, or `not started: 1 more time since … for the same reason` (`, chained as one row within
  24 h` while held), though `ds licence` says `state open`
  (`test_a_range_of_licence_misses_an_evaluation_held_is_chained_once`: the worker's row, at the next
  firing).

**A later vendor build** on a store an open build ran is an evaluation until a licence is installed, and
counts as §7 always did, from `ds.runs`: an evaluation's window is every run ever, the open build's runs
too, so the first two names ever run hold its seats and a third gets M3 at its next run or cycle; a
licence counts the names with a run in its window from `starts`. No `licence.pair` row exists for a pair
first run under the open build: the true-up reads `pairs.counted`. A run made under the open build has a
null record, so no label, and is packed under the licence at issue, as a run from before 010 (D8).
`watch` audits `{from: "open", to: …}`.

**The release** ([RELEASE.md](RELEASE.md)). A checkout whose `SHIPPED_KEYS` is empty releases an open
build: `test_two_vendor_keys` fails a release version that trusts exactly one key, none being an open
build and two or more a vendor build, and §4's bootstrap is no step before a first release. `release
build` then requires the notes to say so, on a line of its own (`release.OPEN_BUILD`), checked after the
notes are found readable (RELEASE §9 step 1), so that `NOTES.txt` and the tag's message say it:

```text
This release is an open build: it embeds no licence vendor key and enforces no licence.
```

Without it: `--notes: a release without vendor keys is an open build: <FILE> lacks the line "This release
is an open build: it embeds no licence vendor key and enforces no licence." (LICENCE.md §19)`, exit 2,
nothing built. A vendor build's notes need no such line and are refused with it, so that notes copied
from an open build's release never say of a build that enforces a licence that it enforces none:
`--notes: a release with vendor keys enforces a licence: <FILE> holds the line "This release is an open
build: it embeds no licence vendor key and enforces no licence." (LICENCE.md §19)`, exit 2, nothing built.
`dsv issue` still refuses a key the checkout does not embed (§14).

**Threat model.** An open build has nothing to enforce, so §18's table is a vendor build's; whoever builds
from source without the vendor's keys runs without limits, by O4.

**Code:** about 50 changed lines in `licence.py`, half of them docstrings (`OPEN`; `judge`'s and
`standing`'s early returns; `limit`, `enterprise`, `record`, `warnings`; I1; M8's tail; `watch`'s log
level; `_installed`'s `problem`), the open line of `ds licence`, `pack.issue`'s null `licence_id`, `open`
in `core/notify.State` with its wording, and `release.OPEN_BUILD` with its two checks; in `web/`, `open` in
`api.ts`'s states and `ui.tsx`'s badge tones, `namedLine`'s open line and `installLine` in
`licensing.ts`, and the Licence screen's two conditions. No migration, no setting, no dependency.

**Documents amended.** Each says what holds in this build, that it needs no licence and enforces none,
and keeps its description of the licence as a vendor build's, marked so: README; SECURITY.md (the
checklist's step 2 is the notes' line, not vendor keys; §10); AUTH §1 ("What each mode is for", "Licences
gate work"), §4.1, §4.2, §6, §8, §9, §12; APPROVALS §1.2, §6.1, §6.2, §10; PACK §1.1, §2.2, §7.2 to §7.4,
§10; RELEASE §9 steps 1 and 8, §10 to §13; ALERTS D6, §2.1 (`open` among the states of `licence.state`,
its wording), §3, §6.2; PRECHECKS D13, §9, §10; READINESS D9, §5 (the score table), §9 (Y12, Y13), §12;
SCHEDULES D9, §7, §8; MCP D10, §7, §11 (T18); SAMPLING D10, §15; ACCURACY D8; BENCHMARK §5;
PENTEST §2, §3, §7; CONTRACTS §1 to §5, §7 to §10; PLAN; architecture §4.3, §9.2 and D.1; DEPLOY (the
introduction, "Sign-in", "The licence", "Notifications", section 4, "Access rules", items 19 and 20 of
the acceptance checklist, section 6, section 9, section 10's table and "The SIEM copy and host
auditing", "Backups and restore", "Upgrading from a release bundle", "Upgrading to licence keys" and
section 11's "The licence" and "Schedules"); RUNBOOK ("Setup", "Sign-in, roles and tokens", "Four-eyes
approvals and sign-off", "Evidence packs", "Sign-off sampling", "Licence", "Triage with table
pre-checks", "Notifications", "Cut-over readiness", "Schedules", "Your own agent (MCP)", "Labelling
against the truth", "Security and privacy", "Troubleshooting", "Try it on the mock pairs"); the
example settings' comments; `ds --help` and `ds licence`'s help; `dsv`'s help (a `keygen` entry embedded
in `SHIPPED_KEYS` turns enforcement on).
