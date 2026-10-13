# Sign-off sampling and sampled evidence packs: contract

This contract is binding. It builds sign-off sampling and sampled evidence packs on docs/architecture.md
§12.2 and §15, and amends [APPROVALS.md](APPROVALS.md), [PACK.md](PACK.md), [LICENCE.md](LICENCE.md),
[RETENTION.md](RETENTION.md) and [TABLES.md](TABLES.md) (§17).

**Goal:** a population too large for a census (20M keys take 11.6 days at 20 req/s) is signed off on a
pre-registered, stratified hash sample nobody chose, judged per stratum by the engine on an exact one-sided
95% Clopper-Pearson rule, and certified in a pack that says what a sample does not prove. **Shape:** a
`hash_sample` key source over a *frame* (a side's table in one snapshot, or as its census record says
where its engine cannot pin it: §3, §11.3; a side's complete key listing; or
one of each); the worker reads the frame, draws a salt of 128 random bits and, in one audited transaction
before any key of the cycle exists, commits the sample, its strata and the salt's SHA-256; keys never
compared are retried, and per-stratum counts, bounds and verdicts are stored once when the cycle ends; an
append-only ledger of every draw of a *population*, whatever spec drew it, closed with attested fix notes,
stops re-testing and cherry-picking. No new dependency (hashlib, heapq, integers); scipy, locked for the
profiler, is a test oracle only. Every claim stays *observed*.

## 1. Decisions (each one seam)

- **D1** The spec's `salt` is the campaign label; the installation adds a nonce per draw
  (`secrets.token_hex(16)`, redrawn until it holds a letter a–f). Nobody picks or shops a salt.
- **D2** No sample is extended and no unresolved key replaced.
- **D3** Only a key the cycle never read comparably is retried (§7): an observed discrepancy is never
  re-read away (stricter than §15 item 3).
- **D4** A census stratum passes on x + unresolved ≤ ⌊target·(N_h − absent_both)⌋.
- **D5** Every stratum dimension of a hash sample has buckets: labels are spec constants. A key's label is
  still a customer attribute (`flagged=Y`), shown only in aggregate (§11.5).
- **D6** The ledger belongs to the population (§9), not the spec name: a draw that did not pass is closed by
  a person with a fix note before a later draw over an overlapping population is signed off, and a later
  failure spends an earlier pass.
- **D7** A sampled soak rolls; a boundary after a draw that did not pass pauses it until that draw is
  closed.
- **D8** At most one live `bottom_k` run per population, whatever the spec name.
- **D9** A new approval scope, `sampling`, pre-registers frame, strata, sizes, label and retries.
- **D10** No new licence fence (O2); a Pro fence would be one line in `Standing.refusal`. An open
  build has no licence fence at all ([LICENCE.md](LICENCE.md) §19).
- **D11** Every new sign-off records attests format 2; packs stay `deltasleuth-pack/1`, the certificate
  naming the audit row an auditor compares by hand (§11.4).
- **D12** Labels live on `ds.run_keys` only and never join `list_results`; results and findings reach them
  only through the judging query (control-plane.sql copies them).
- **D13** `truncated` (`api_uncomparable`) is not unresolved, as §15 item 3 says: a stratum holding one
  fails until a paginated fetch covers it (G11).
- **D14** `absent_both` is gated at the stratum's own target, counted in the worst-case bound and named in
  the claim (stricter than §15's 0.1% for high-risk strata).
- **D15** A sign-off bucket no frame key falls into fails the draw (W9): an empty stratum certifies nothing.

## 2. The spec

### 2.1 What the loader accepts

`spec.schema.json` (both copies) gains, and `rules._gating` stops refusing:

```yaml
key:
  source:
    kind: hash_sample
    method: bottom_k                  # rate: monitoring only, never signed off (G2)
    salt: cutover-q4                  # campaign label, now ^[A-Za-z0-9_.+-]{4,64}$
    resolve: {times: 3, delay: 10m}   # new: retries per sign-off member, 0..10 (default 3); delay default 10m
    strata:
      - {name: degree, column: degree, buckets: [{label: d0, max: 0, targetSize: 3011}, …, {label: other, targetSize: 3011}]}
      - {name: flagged, column: flagged, buckets: [{label: Y, values: ["Y"], targetSize: 30107, riskTier: high}, {label: N}]}
      - {name: recent, cel: "row.changed_at != null && timestamp(row.changed_at) >= now - duration('168h')",
         buckets: [{label: Y, values: [true], targetSize: 30107, riskTier: high}, {label: N}]}
    signoffStratum: "riskTier == 'high' ? (strata.flagged == 'Y' ? 'flagged=Y' : 'recent=Y') : 'degree=' + strata.degree"
    source:
      kind: union
      sources:
        - {kind: table, side: a, strataColumns: [degree, flagged, changed_at]}
        - {kind: paginated_endpoint, target: new-api, request: {…}, keyPath: $.id,
           pagination: {kind: cursor, …, maxPages: 30000},
           strataFields: {degree: $.degree, flagged: $.flagged, changed_at: $.changedAt}}
```

- **`keySourceTable`** `{kind: table, side: a|b, strataColumns?: [columnName]}`: that table side's
  population (relation, `table.where`, key maps, which `population_filters` approves), read as the key census
  reads it (PACK §1.3); only as `hash_sample.source` or a union member.
- **A frame** is one member or a union of two, one per side: a `table` of a table side, or a
  `paginated_endpoint` whose `target` is that side's (side a's when both sides name it). The first member in
  `sources` order yielding a key gives its strata row.
- `CEL_VARIABLES["stratum_dimension"]` gains `key: STRING` and `now: TIMESTAMP`, `["signoff_stratum"]`
  gains `now`: the frame's read start (the snapshot's, or the listing's first page), so a service side
  without listed fields stratifies on the key and recency counts from the cut (cel-expr-python evaluates TIMESTAMP variables, checked).
- `CompiledSpec.sample: Sample | None` (`core/types.py`, `assemble.py`): method, label, rate thresholds
  `int(Decimal(str(rate)) * 2**64)`, dimensions with ordered buckets (a sign-off bucket without `riskTier`
  is `standard`), `signoffStratum`, `resolve`, members and `population` (§9). Tiers are the build's:
  `core/sampling.TARGETS = {standard: 1/1000, high: 1/10000}`, confidence 19/20, margin 1/200, copied into
  every draw.
- `approvals.SCOPES` gains **`sampling`**, which `needs(spec)` returns when `spec.sample` is set: under SSO
  a non-author approves frame, strata, sizes, label and `resolve` before any run draws, and the sign-off
  cites that approval (`_cited`), its pre-registration. Its `review` adds `ledger`, the population's draws
  under any name (§9).

### 2.2 What the loader refuses (`rules._gating`, new `rules._sampling`)

Each `unsupported` message starts `not supported: `.

| Code | Case | Message |
|---|---|---|
| `unsupported` | csv, sql, run_keys (as today); a `list` in a frame | `hash_sample over an inline list; sample a side's table or key listing` |
| `unsupported` | a `table` naming a `side`, or a `union`, outside a hash sample | `a table key source outside hash_sample (a table side's keys are scanned, or checked against it)`; `a union outside hash_sample; wrap it in a hash_sample` |
| `unsupported` | a key table (a `table` with a `target` and a `table` of its own: [TABLES.md](TABLES.md) §2) in a frame | `a key table inside hash_sample; a frame's table is a side's` |
| `unsupported` | two table sides | `hash_sample between two table sides: their scan compares every row` |
| `unsupported` | a composite key beside a service side (a multi-part key) | `hash_sample with a composite key beside a service side` |
| `unsupported` | in a spec with a `hash_sample` source only, `knownDiffs/*/when` or `compare/valueFilter` reads `stratum` or `strata` | `a predicate over stratum or strata; strata label a sample's keys, not their findings` |
| `frame_member` | a member | `side b is not a table side`; `the frame's listing reads target master-data, which is neither side's: each frame source enumerates side a's or side b's keys`; `union sources 0 and 1 both enumerate side b: a union takes one source per side` |
| `stratum_needs_buckets` | a dimension | `a stratum dimension of a hash sample needs buckets: a value read from the data would become a label in reports and packs (a bucket without values, min or max catches the rest)` |
| `strata_column_missing` | a member | `the strata read column 'degree', which source 1 does not expose (strataColumns or strataFields)` (a dimension's `column`, or `row.<name>` in a dimension's CEL or `signoffStratum`) |
| `sample_cut` | `execution/stop/maxKeys` | `maxKeys would cut the sample: size it with targetSize or rate` |
| `sample_size` | Σ targetSize > 1,000,000 | `the sample would hold 1,204,000 keys, over 1,000,000: compare the population as a census` |
| `stratum_label_long` | strata | `the longest stratum label these dimensions make holds 257 characters, over 256` (the schema's rule, now enforced) |
| warning `undersized` | a `bottom_k` bucket whose `targetSize` is under its tier's `zero_failure_n` | `bucket degree=d0 (standard, 0.1%) draws at most 2,000 keys; a zero-failure pass needs 2,995 cleanly compared keys (draw 3,011): it passes only as a census` |
| warning `label_key_shaped` | a bucket or salt label, value-class keys | `the label cutover-2026 (salt) is shaped like a key of this spec: a pack would refuse it (docs/mvp/PACK.md §2.3); rename it`, a bucket's `(a bucket of degree)` (each part through `keys.key_matches`) |
| warning `value_key_shaped` | a bucket's `values` item, `min` or `max`, a string or a number read as its text, value-class keys | `the value 40012345 (a bucket of watch) is shaped like a key of this spec: a pack would refuse it (docs/mvp/PACK.md §2.3); bucket without customer keys` (each part through `keys.key_matches`) |

Other specs compile as before: `CEL_VARIABLES` keeps binding `stratum` and `strata` (as None), and no stored
body has a `hash_sample`, `table` or `union` source (rules refused them before this build), so `ctx.spec`
recompiles every stored spec unchanged; a test compiles one whose `knownDiffs/*/when` reads `strata`.
`sampling/mode: hash` stays unsupported: a rolling hash-sample soak already draws a new sample every cycle.

## 3. The frame (`runner/sample.py`)

A sample is only as good as its frame, so only frames whose completeness the product observes are accepted,
read from the spec's declared targets only (the black-box principle). Members are read in order; each key
passes `key.normalize`, `key.pattern` and the store's limits (`keys.key_matches`, no NUL, ≤ 1024 bytes). A
key that fails is *unfit*: counted per member, never shown, left out. Unfit keys refuse a sign-off (G4), as
PACK §1.3 counts them missing.

| Member | Read | Complete when | Census record |
|---|---|---|---|
| `table` | new `queries.frame_sql(side)`: `census_sql`'s canonical key and order plus `to_jsonb(r.*)::text` of a lateral `SELECT` of the `strataColumns` (as `rows_sql` builds documents), tie-broken by that text; one `pg.snapshot(target, timeout_s=census.TIMEOUT_S)`, a named cursor, the engine's `BATCH` rows a fetch, `null_keys_sql` for `skipped` (through the side's engine: `Engine.snapshot`, `Engine.frame`, `Engine.null_keys`). `tables.plan` resolves and grants strata columns as filter columns (`strata` in the side plan: name as written -> column, only on a table side a frame member reads strata from, so no other run's stored plan changes) | read to its end; several rows of a key count once (the first gives its strata; the compare reads it `identity_ambiguous`); keys out of order refused, as in `census._count` | `{basis: "frame", target, relation, where, rows, skipped, keys, unfit, at}` |
| `paginated_endpoint` | new `RestConnector.walk(side_cfg)`, yielding each page's items under the target's rate budget (each page admitted by the breaker on its own); `PageWalk(merge=False)` keeps only the previous page's identities (not `_seen`) and caps each page, never a merged body, at `max_body_bytes`; an unparseable page 1, or page 1 with duplicate member names, stops it (no `observe()` judges a listing's pages) | `eof`, not `unstable`, no failed page, no key listed twice on any pages; the walk's judgement (loops, `maxPages`, declared `stable_ordering` for window kinds) unchanged; the walk raises `ListingIncomplete` (W1's text) once it proves incomplete | `{basis: "listing", target, items, keys, unfit, first_at, last_at}` (`first_at` before page 1 is requested, as `keys._listed` records it) |

**Dedupe.** A frame of one `table` member needs none: census order makes its keys distinct. Otherwise the
draw's own autocommit control-plane session sets `temp_buffers` for itself (`set_config`, not local to a
transaction) to the setting `sampling.temp_buffers` (default `2GB`, DEPLOY §10) before `CREATE TEMP TABLE frame_keys (h uuid PRIMARY KEY, member smallint NOT
NULL)`, `h` being the first 16 bytes of the key's selection digest (§4): keyed by the secret nonce, so no
brute-forceable digest is stored, and gone with the session. Batches are 10,000 keys, deduplicated in Python first. Member 0
only inserts (`INSERT … SELECT unnest(%s::uuid[]), 0 ON CONFLICT DO NOTHING`; a listing's rows not inserted
are repeats, W2); member 1 marks the digests of a batch member 0 gave (`UPDATE frame_keys SET member = 2 WHERE
h = ANY(%s) AND member = 0 RETURNING h`), each counted once, as `both`, then inserts the rest as member 1: a
digest already there (member 1's, or marked) is a repeat (W2), on whichever page, of whichever key. **Strata rows** are `strataColumns`, `strataFields` (first
JSONPath match, null where an item has none) or `{}`, numbers exact (`jsonio`). A member record's `keys`
counts the distinct fit keys it gave, those another member gave too included; the draw's `frame.origin`
splits the frame's keys into those only side a's member, only side b's member, or both gave. A listing
that lists a key twice is refused as W2 even when its walk is also unstable (a repeated item makes it
so), and as W1 otherwise. W6's position is the key's among the distinct keys its source gave the frame.

**The pack's census.** The draw stores `{cycle, at, a, b}` by `repo.set_census`, a record per side a member
enumerates, so `census.of_cycle`, `pack._counted` and `_uncounted` read a frame as a census;
`census.described` gains `its population read to its end in 1 snapshot at <t> as the sampling frame`.

**A frame on MySQL or Oracle** ([ENGINES.md](ENGINES.md) §10, §11) is the same stream from that engine:
the key texts in code-point order, then the strata columns' rendered texts, from which the worker builds
the strata JSON, ties broken by those texts in binary order; a LOB, JSON or `other` strata column is
refused when the run is created (`side a: strata column 'doc' (CLOB) cannot stratify a frame`). The
selection digests are Python's and do not change. Its record adds `engine` and `consistency`, so a frame
read from a MySQL view or a table outside InnoDB says so, is warned of on the certificate (PACK §1.2) and
is not said to be read in one snapshot by the pack's claim (§11.3); the record of every table side whose
plan names an engine also adds `server`, the major and minor version of the server the frame's session
met (ENGINES §13).
**A frame of files** ([FILES.md](FILES.md) §11, §12; a mixed pair's, since a hash sample between two
tables is refused) is the same stream from the files engine: one statement sorts the side's keys with
their strata columns in DuckDB, holding the process's one sort, the files stat-checked after each
fetched batch and hashed again when the rows end. Its record holds no snapshot and no digest: `its
population read to its end from 12 fixed file(s), 3,650,722,201 bytes, pinned … and hashed the same when
the read ended at …; an export, not the system it came from: …; as the sampling frame`, with `rejected`
(the lines of its files that were not rows) and `repeated: null` (a frame counts no repeated key). A
frame of side a whose files held lines that were not rows is no census, so its cycle gets no pack (PACK
§1.2). The pack's claim says of such a frame `a table in one pass not observed to be a snapshot`
(§11.3): its record's `consistency` is `fixed`, not `snapshot`. The sample's drawn keys are then read
from the files 200 a statement, each one pass over them (FILES §12). A frame whose files changed fails
its read as any read of them does.
A frame that loses its snapshot, whose login is refused, or that meets another server code pauses the run:
`the frame of side a could not be held in one snapshot (ORA-01555)`, `the frame of side a could not be
read: the target refused the credential (ORA-01017)`, `the frame of side a could not be read (<code>)`.
`census.pending` and its SQL twin `repo._CENSUS_PENDING` (`census_due`, `lease_due(census=True)`) exclude
`keys_source = 'hash_sample'`: a key census would count every unsampled key missing.

## 4. Selection (`core/sampling.py`, pure)

**Salt and u.** `salt = f"{label}:{nonce}"`, `salt_sha256 = sha256_hex(salt)`; the nonce lives only in the
draw task until the committing transaction. `digest = sha256(f"{salt}\x1f{key}".encode()).digest()`
(`adjudicate._hash`'s form), `u = int.from_bytes(digest[:8], "big")`; label and nonce hold no U+001F, so the
encoding is unambiguous. Keys rank by `(u, key bytes)`.

**Strata**, once per distinct frame key, over `key`, its row and `now`, never fetched documents: each
dimension takes its column or CEL value (new `predicates.label`, `test`'s twin for values: a string, boolean,
null or exact number, as §2.1's `recent` dimension returns a boolean; failing closed on double ambiguity); the
first matching bucket wins (`values` as JSON values, `min`/`max` inclusive in Decimal, null only to a
catch-all), and a value no bucket takes fails the dimension (W6). `stratum` is `dim=label|…`; `riskTier` is
high if any matched bucket is. A `rate` sample without strata has no label (NULL, which `run_keys`' checks
allow and an empty label would not): every key it chooses is contrast and it stores no strata records.
`signoff_stratum` comes from `signoffStratum`, memoised per `(strata, riskTier)` when `expressions.reads`
finds it reads neither `key` nor `row`; without it, `bottom_k` takes the first matched `targetSize` bucket,
high first (a high-risk key whose high-risk buckets have no `targetSize` fails as W4), and `rate` the whole
stratum label. A `bottom_k` stratum's tier, hence its target, is its sign-off bucket's `riskTier`, never
the tier of the first key the frame gives; W4 keeps every key's tier its bucket's.

**Selection.** `bottom_k`: each sign-off bucket keeps a max-heap of its `targetSize` smallest `(u, key)`
and counts `N_h`; `N_h ≤ targetSize` is a **census** (all drawn). `rate` (and contrast under `bottom_k`):
`u < threshold` of the effective rate, the highest of the base rate and every matched bucket's; rate 1.0 is
a census. **Membership:** `signoff_member` only for keys their own bucket chose (its heap, or `u`
under its own bucket's rate, else the base rate); others are *contrast*: compared, reported, never bounded.
`seq` orders the sample by `(⌊u·2^64 / m⌋, key)`, `m` being the bound that chose the key (its bucket's
`u_max` + 1, 2^64 for a census, or its rate threshold), so every stratum advances at the same pace and a
stopped cycle has compared a uniform sub-sample of each; `u_max` is a stratum's largest member `u` (a
decimal string); `x_max`, the largest x `passes` allows at the drawn size (−1 if none; 0 at 3,011 and at
30,107), or ⌊t·N_h⌋ for a census, is found from `upper` and confirmed by two exact `passes` calls, and
stored with the stratum; `selection_sha256` hashes the lines `seq⇥key_masked⇥stratum⇥signoff_stratum⇥member`
in `seq` order (`sampling.line`: a NULL label empty, `member` `true` or `false`, each line ending in a
newline). Label errors (W3–W6), a sign-off bucket without keys (W9) and over 1,000,000 keys (W7) fail
the draw, aggregated, never naming a key. CPU runs a batch at a time in `asyncio.to_thread`; memory holds
heaps, contrast candidates and at most 4,096 memoised bucket lookups (`_MEMO`, cleared when full), never the
frame. Given a uniformly random salt and SHA-256 taken as a random
function, bottom-k per stratum is a simple random sample without replacement.

## 5. Draws and soaks

**Creation.** `runs.create_run`, for a hash sample, gates and admits as today and refuses explicit keys
(R4), a soak that does not roll (R2), file mode (R3), and a `bottom_k` spec whose population overlaps a live
`bottom_k` run's (queued, running, paused or cancelling; R1, whatever its spec name), checked again in the
insert transaction under `pg_advisory_xact_lock(0x4453534D, hashtext(id))` ("DSSM") for each population id,
in sorted order. It inserts the run `running` (`params.keys_source = "hash_sample"`, `params.method`,
`params.population`, no keys, no salt, `runs.draw = {due: now}`); `POST /v1/runs` answers 202 at once.

**Worker.** `_Worker._sweep_once` starts a `self.draws` task per run `repo.draws_due` finds (running,
`draw.due ≤ now`). `repo.cycle_done` is false while a *running* run's `draw` is set, so a run stopped while
drawing ends its cycle. `sample.draw` reads the frame, selects, masks and seals the chosen keys, then in one
`repo.audited` transaction: takes the run's row lock (`set_run_status(…, when=["running"])`); checks the
cycle unchanged and `draw` still set; inserts `ds.sample_draws`, whose trigger refuses a draw not made as of
`now()` for a running run's current cycle before its keys; inserts the labelled keys (`_insert_keys` gains
three arrays); `set_census`; clears `draw`; audits `sample.draw` last. A run stopped meanwhile discards the
uncommitted draw: nothing was disclosed. The frame's read checks the run as each batch or page arrives, at
most every 5 s (`sample.WATCH_S`), and stops silently, closing its snapshot, walk and session, once the run
is no longer running, has moved on to another cycle or has no draw pending. Under
[PRECHECKS.md](PRECHECKS.md) §7 (V3) a sampled run with a table side waits, before each cycle's draw, for
the schema guard's look at that cycle's start (two tries at most; a look that cannot be made is recorded
and the draw goes on): a change that breaks the run's plan pauses the run (`run_paused_schema_drift`)
before its draw reads the frame.

**Ends.** Every path that ends a drawn cycle stores its judgement (§8.1) in `ds.cycles.sample` through
`repo.end_cycle(…, sample=)`: `finish_cycle` (summarised, and an erasure's cycle, judged before its data
goes), `purge._abandon`, `purge._end_for_erasure` and `fail_run`, which judges on a connection of its own
first (`sample.judgement`); the others call `sample.ending(ctx, conn, run)`, which reads the cycle's draw
(`repo.get_draw`) and judges it in a savepoint, so the caller's transaction outlives a failed judgement.
Judging (`sample.judge(conn, draw, spec)`: the draw names run and cycle, the spec gives the recheck
outcomes and `resolve.times`) reads stored rows and needs no key; a judgement that raises is logged and
leaves the cycle `unjudged` (§9). A cycle that ended undrawn (stopped while drawing) has no draw and a
NULL `sample`.

**Failures.** A `sample.DrawError` (W1–W9, a safe text) pauses the run as `system:worker` with `DRAW_PAUSE =
"run_paused_draw"`, sets `draw.error` and `draw.at` and audits `paused {reason}` (as every pause is) then
`sample.draw_failed {cycle, error}`; `resume_run` clears it (an `unread` pause), keeps `draw.due` and the
next sweep draws with a fresh nonce. An unreachable database (`psycopg.OperationalError` but
`QueryCanceled`, a W8) retries after `worker.lease_seconds`; a missing KEK pauses as elsewhere; a table frame
refused with a code of ENGINES §11's denied class (a column dropped since the run was planned) first has the
schema guard look (PRECHECKS §7, V5): a change that breaks the run's plan pauses it, its draw still due;
anything else fails the run, as a bug.

**Soaks.** A rolling boundary opens the next cycle with no keys (`_rolled` returns `{}`) and `draw = {due:
next_at}`, skipping `census.listed`: every sampled cycle has its own draw and no sample is re-tested. When
the ending cycle's `bottom_k` draw's judgement is not `passed` (unjudged included), the next cycle starts
paused (`SAMPLE_PAUSE = "run_paused_sample"`, audited `paused {reason, approval_id, draw_id}`; a licence's
or an approval's reason at the same boundary wins, as today) and `resume_run` refuses (R5) until that draw
is closed, so the fix and its close precede the next draw. R5 is judged whatever the pause's reason: while
a sampled run's current cycle is still to be drawn, the `bottom_k` draw of the cycle before must have
passed or be closed. A rate draw never pauses a soak: it is never closed (C4). A boundary paused for the
licence or an approval draws after its resume. Watching fixed keys converge is an explicit-keys run of a spec without a sample.

**Schedules** ([SCHEDULES.md](SCHEDULES.md) §7). A schedule holds no salt, nonce or selection: each
scheduled run of a hash sample is created undrawn, as above, and drawn by the worker with a new nonce
(D1). Saving a schedule checks R2 (`sample.refuse(mode, None, None, soak)`); R3 and R4 cannot arise, a
schedule having no keys and no samples. Each firing meets R1 and R2 in `create_run`: R1 (a live
`bottom_k` run samples the population) is tried again every minute while the time's window is open,
then recorded as `schedule.missed {reason: sample, code: live}`; any other refusal is `refused` at
once. Before creating the run, the firing reads the population's ledger: while it holds a draw
`sample.blocking` flags (ended, `bottom_k`, did not pass, not closed), nothing is started (`unclosed`,
naming the draw), which is D7's boundary rule for firings no person attends. A rate sample never
blocks a firing.

## 6. Sizing

`zero_failure_n(t)`, the smallest n with `20·(b−a)^n ≤ b^n` (t = a/b), searched exactly around ⌈ln 0.05 /
ln(1−t)⌉, gives **2,995** (0.1%) and **29,956** (0.01%); `attempted(n) = ⌈200n/199⌉` gives **3,011** and
**30,107**. The margin lets 16 standard (151 high-risk) first-pass keys go unresolved, absent or truncated
before a zero-failure bound fails; §8's gates allow far fewer after retries. §15's 12 + 3 strata make
**126,453**, a spec, not code, once their columns exist (§19). `ds samples size --tier standard|high
[--failures N]` (no settings) prints the single-stage sizes: `high (≤ 0.01%, one-sided 95%): 47,437
cleanly compared keys pass with at most 1 discrepant; draw 47,676 (0.5% margin); fix the size before any key
is compared, never extend it`; a size over 1,000,000 exits 2: `5,000 discrepant key(s) pass a 0.1% target
only among over 1,000,000 cleanly compared keys: compare the population as a census`.

## 7. Retries to resolution

**What.** `sample.retriable(result, before)`: a `signoff_member`'s final result (no recheck owed) is retried
while `retries < resolve.times` only when it is INCONCLUSIVE for a reason in `ERROR_REASONS`,
`unstable_pagination` or `recheck_failed` **and** `before`, the stored result the last pass judged (the
result itself on a first read), is INCONCLUSIVE for a reason in `ERROR_REASONS` or `unstable_pagination`:
no read of this attempt was comparable, and the read that failed may compare when read again. (An
unusable `before` alone would retry a key whose rechecks failed on a read no retry changes, `truncated` or
`identity_ambiguous` say, which the list below never retries.) A usable read, once stored, stays the stored
result until the key is final (`recheck.confirm` returns `first` only for an unusable re-read), so a key
whose comparable read found a discrepancy and then ended `recheck_failed`, DRIFT(a_changed) or
DRIFT(b_converged) is never retried, and a key that read MATCH is never re-read: a retry only gives a
comparison to a key that had none, and never removes a discrepancy. Every other outcome and reason is
never retried and stays unresolved, absent or truncated: `absent_both`, `skipped`, `truncated`, `unparseable`, `resource_limit`,
`schema_drift`, `absence_unconfirmed`, `watermark_missing`, `duplicate_member_name`, `identity_ambiguous`
and every DRIFT reason (`changed_since`, `a_changed`, `b_converged`, `watermark_mismatch`,
`skew_exceeded`, `cut_window`). Contrast keys, unsampled runs and `times: 0` behave as today.

**How.** In `_Worker._process`, a done key that is retriable goes to `repo.schedule_retries`, not
`complete_keys`: `state = 'pending'`, `pass_no = 1`, `retries + 1`, due after `resolve.delay` plus jitter,
and the replaced result's reason appended to `results.attrs.retried`. `lease_due` returns `signoff_member`
and `retries`. A retry's first read is a first read: `save_key_results` takes `fresh` (pass 1 of a retry)
and then sets `first_outcome` from it, and every write keeps `attrs.retried`, so `counters.owed` and the
retry's own rechecks judge it exactly as they judge a first read. `StopMonitor` counts `first_read =
pass_no == 1 and retries == 0`. In-cycle retries keep one draw, one cycle, one sign-off: no second run to
choose from.

## 8. Judging each stratum

### 8.1 Counts

`sample.judge(conn, draw, spec)` reads one grouped query, `repo.stratum_counts`: the cycle's `run_keys`
LEFT JOIN `results`, grouped by `signoff_stratum`, `signoff_member`, `state = 'done'`, `retries > 0`,
outcome, first outcome, confirmation, reasons (with the sides an INCONCLUSIVE result is `truncated` on,
whatever its reason) and a `bug` flag
(EXISTS a finding of the key under a `bug` rule). `counters.count_groups`, `count_cycle`'s grouped twin, builds each sign-off stratum's counters over its members (`total` =
members) and checks the same identities (`ReconciliationError`); members plus contrast equal the cycle's
keys. **n** = `compared` (MATCH, NOISE_ONLY, KNOWN_DIFF, MISMATCH, NOT_FOUND_A/B); **x** = `different`
(MISMATCH, NOT_FOUND_A/B, KNOWN_DIFF under a `bug` rule); both also count a *done* member `count_groups`
counts `outstanding` (its first read unusable, its one comparable read discrepant, no recheck left): an
observed discrepancy is never left out (D3). **absent_both** and **api_uncomparable** (an INCONCLUSIVE
member truncated on a side, whatever its reason: a truncated read whose recheck is truncated again ends
`recheck_failed` and still counts here, since no re-read lifts an API cap, D13) apart;
**unresolved** = `indeterminate − absent_both − api_uncomparable`. `count_groups` omits the counters only
findings give (`suppressed_by_rule`, `normalization_only_keys`, `schema_drift`). A stratum holds `{label,
tier, target, basis, frame_keys, drawn, contrast, x_max, counters, n, x, unresolved, absent_both,
api_uncomparable, retried, ub95_one_sided, ub95_one_sided_all_suppressed, ub95_one_sided_worst_case,
verdict, why}`, `why` listing the refusals it fails as G5–G8 and G11 word them after `stratum <label> of
cycle <c> of run <R>` (G5's tail: `is over its 0.1% target: its one-sided 95% upper bound is …`; §8.2
says which); the
judgement adds `draw_id`, `status`, `contrast` (the contrast keys' counters, for the `(contrast)`
coverage row) and `weighted_rate` (Σ (N_h/N)·x_h/n_h to 6 significant digits, informational; null
without strata or when a stratum has n = 0), never a pooled bound. It is stored once, in
`ds.cycles.sample` (§5 Ends): reports, attests and packs read it, never recount it.

### 8.2 Verdicts

u_f counts a stratum's members that are `done` with an unresolved result: final, since a retriable key is
pending again; ab_f and api_f count its `done` members absent on both sides and above the API cap, final
too: a first read still due its recheck may yet find the key or compare it.

| Stratum | `passed` when | `failed` for certain (also partial or live) |
|---|---|---|
| sampled | `passes(x, n, t)`, unresolved ≤ ⌊t·n⌋, absent_both ≤ ⌊t·drawn⌋, api_uncomparable = 0 | x > `x_max` (`passes` rises with n); ab_f > ⌊t·drawn⌋; api_f > 0; u_f > ⌊t·(drawn − u_f − ab_f)⌋ |
| census | x + unresolved ≤ ⌊t·(N_h − absent_both)⌋, absent_both ≤ ⌊t·N_h⌋, api_uncomparable = 0 | x + u_f > ⌊t·(N_h − ab_f)⌋; ab_f > ⌊t·N_h⌋; api_f > 0 |

A stratum is complete once every member is done; only a complete stratum passes (one a stopped cycle left
with members never read is `unfinished`, even when its compared keys would pass). A complete stratum that
does not pass is `failed`, and so is any stratum failing a gate for certain; any other is `unfinished`.
`why` holds a failed stratum's certain failures and, once complete, every gate it does not hold; the
judgement a cycle's end stores (`judged(…, ended=True)`, from `sample.ending`) words every gate an
unfinished stratum does not hold too, so its refusal shows why, while `progress` words only certain
failures. The judgement's `status` is `passed` (every stratum, the cycle complete), `failed` (any) or
`incomplete`. A
census never takes the binomial bound (at N_h = 5,000, x = 0 it would be 0.05990%, failing 0.01%): its
three bounds are null. Reported, never gated:
`ub95_one_sided_all_suppressed` (x′ = n − `by_outcome.MATCH`: every KNOWN_DIFF and NOISE_ONLY) and
`ub95_one_sided_worst_case` (x + unresolved + absent_both + api_uncomparable over n plus the same). Contrast
keys never enter a bound; their discrepancies are root causes the sign-off decides, and unexplained
CRITICAL counts every key (APPROVALS §1.2 item 5).

### 8.3 The exact decision and the shown bound

With t = a/b and c = b − a, `passes(x, n, t)` is false when n = 0 or x·b ≥ n·a, and otherwise holds exactly
when `20 · c^(n−x) · Σ_{k≤x} C(n,k)·a^k·c^(x−k) ≤ b^n`: BinomCDF(x; n, t) ≤ 0.05 in integers, hence the
same test as `beta.ppf(0.95, x+1, n−x) ≤ t` (the CDF falls as p rises). Measured: 0.03 s at (x 1, n
47,437), 0.2 s at (199, 200,000), 2.6 s at (999, 1,000,000); judging calls it only when x ≤ `x_max`. The
shown `upper(x, n)` is 1 − 0.05^(1/n) at x = 0, 1 at x ≥ n, else a bisection on a log-space binomial CDF; it
matches scipy 1.18.1's `beta.ppf` within 3e-15 on every row below, is rounded up to 4 significant digits
(`ROUND_CEILING`), clamped to the target when `passes(x, n, t)` holds and raised to the next 4-digit value
above it when it does not and the bound rounds to the target or below (float error at the boundary), so it
never contradicts the exact decision (a stratum failing only another gate shows its bound as it is), and
stored as a decimal string, never recomputed; the two reported-only bounds likewise, each against its own
`passes`.

| x | n | `beta.ppf(0.95, x+1, n−x)` | Shown | ≤ 0.1% | ≤ 0.01% |
|---|---|---|---|---|---|
| 0 | 2,995 / 2,994 | 0.000999744421 / 0.001000078170 | 0.09998% / 0.1001% | pass / fail | fail |
| 0 | 2,980 | 0.001004774162 | 0.1005% | fail | fail |
| 2 | 2,995 | 0.002100594339 | 0.2101% | fail | fail |
| 1 | 4,742 / 4,741 | 0.000999998368 / 0.001000209210 | 0.1000% / 0.1001% | pass / fail | fail |
| 5 | 10,511 / 10,510 | 0.000999931315 / 0.001000026431 | 0.1000% / 0.1001% | pass / fail | fail |
| 0 | 29,956 / 29,955 | 0.000099999415 / 0.000100002753 | 0.01000% / 0.01001% | pass | pass / fail |
| 1 | 47,437 / 47,436 | 0.000099999522 / 0.000100001630 | 0.01000% / 0.01001% | pass | pass / fail |
| 3 | 77,535 | 0.000099998953 | 0.01000% | pass | pass |
| 2 | 2,997 (worst case: 0 + 2 unresolved) | 0.002099193546 | 0.2100% | fail | fail |
| 15 | 2,995 | 0.007701469069 | 0.7702% | fail | fail |

The two-sided 95% bound at x = 0, n = 2,995 is 0.1231% and would fail 0.1%; only the one-sided bound is
computed.

### 8.4 What the statistics mean

A sign-off passes only if every stratum does (an intersection-union test): with any stratum above target it
passes with at most that stratum's 5%, so no multiplicity correction and no pooled bound.
Without replacement, the binomial bound is conservative in the pass region
(hypergeometric P(0 | N 5,000, D 5, n 2,995) = 0.0103 ≤ 0.999^2995 = 0.0500). Leaving unresolved and absent
keys out is valid only if they are missing at random; retrying only never-compared keys, the gates at the
target share and the worst-case bound (about twice the target, the guarantee under adversarial
missingness) are the defence.

## 9. Draw discipline: the population's ledger and closes

**Population.** `Sample.population`, computed by the loader without reading a target, is the frame's member
ids, sorted: `table:<target>/<name>` (the table name lower-cased, its schema left out) and
`listing:<target>:<METHOD> <path>`, the path as RFC 3986 §6.2.2 compares paths (`assemble._member`: a
percent-escape of an unreserved character decoded, any other's hex upper-cased, so `/list`, `/%6Cist` and
`/%6cist` are one id; the request still sends the path as written). Schema, `where`, filters, query and body are left out, so every
sub-population of one relation or endpoint shares a ledger. Two populations overlap when they share an id.
It is stored in `runs.params.population` and `ds.sample_draws.population`.

**Ledger** of a draw or a spec name (its newest version's population): every `bottom_k` draw of any spec
whose population overlaps, with its status: `live` (its cycle has not ended), `passed`, `failed` or
`incomplete` (its `ds.cycles.sample.status`), or `unjudged` (ended without a judgement). Rate draws are
listed as monitoring, never blocking, never signed off. Order is commit order, never `now()`: the chain
seqs of the `sample.draw` and `sample.close` audit rows (009 chains rows in commit order under the DSAU
lock; `repo.ledger`). A draw no committed `sample.draw` row names (the row altered since) has no place in
that order: R-a and R-c leave it out, and its own sign-off and, for a `bottom_k` draw, the sign-off of
every other draw of its population, before or after it, are refused (`the sample D-… of cycle 2 of run R-…
does not match its sample.draw audit row: its order in the ledger is unknown`, from `sample.problems`); its
pack also gets P2. **Close:** `POST /v1/samples/close {spec_name, note}` (an operator who is a signed-in
person) closes every ended, unclosed, non-passing `bottom_k` draw of that name's ledger, whatever spec drew
it (`sample.blocking`): `closed_by`, `closed_at = now()`, `close_note`, audited `sample.close {spec_name,
note}` per draw, in draw-id order, which the answer lists. The note is a `ds.note`, checked by
`rawvalues.key_shaped` as revoke reasons are (`D-` ids are generated text, §11.5) against every spec whose
later sign-offs could attest it: the named spec's newest version, the newest version of each spec name with
a draw on the ledger or sampling an overlapping population (`sample._sharing`, drawn or not), and the spec of
each draw it closes (a spec stored after the close is not known to it: a pack of that spec whose gate finds
the note key-shaped is refused, naming it); it says what was fixed, which the approver judges (the engine cannot see a fix). R-a and R-c
weigh the other `bottom_k` draws only: a rate draw is monitoring. A draw D, its `sample.draw` at chain seq
s_D, is signed off only under:

- **R-a** every other ledger draw whose `sample.draw` precedes s_D (each ended before D was drawn, by D8)
  passed, or its `sample.close` precedes s_D; an incomplete or unjudged one needs a close too, so stopping,
  failing or abandoning a draw until a selection suits is recorded, not free;
- **R-b** D's own cycle passed: a failed sample is spent, never re-tested (a cycle whose judgement raised
  when it ended is refused: `the sample D-… of cycle 2 of run R-… ended without a judgement: draw again`;
  one whose judgement is `incomplete`, a member not final when it ended, waiting for a retry or a recheck
  say, though every key holds a result and the cycle's counters cancel and owe nothing, is refused after its
  unfinished strata's gates: `the sample D-… of cycle 2 of run R-… ended incomplete (stratum degree=d0
  unfinished): its cycle ended before every key was final; draw again`);
- **R-c** at the request and at each approving step, no ledger draw after s_D is `failed`, `unjudged`, or
  live with a certain failure (§8.2); later passes and incomplete or live draws never block and are shown
  (review, certificate). The final approving step records the ledger draws after s_D as it judged them in
  its `approval.grant` detail, `later_draws: [{draw_id, status}]`; a pack reads that row, never re-judges
  R-c;
- **R-d** the cycle's keys re-hash to D's `selection_sha256`: nothing extended, trimmed or relabelled.

R-a's facts are frozen by chain order and attested, so a rebuild is stable across later draws, closes and
purges (`ds.sample_draws` and each cycle's `sample` outlive purges).

A scheduled draw is a ledger draw like any other ([SCHEDULES.md](SCHEDULES.md) §7): one that does not
pass after D refuses D's sign-off under R-c (D6), so a schedule is paused before its sign-off is
requested, and a `bottom_k` draw that did not pass stops the schedule's later firings until it is closed
(§5: a rate sample's draws never stop one).

## 10. Sign-off (APPROVALS amendments)

**Attests format 2** (`approvals._format_2`; `rebuilt` gains `case 2`, formats 0 and 1 rebuilt as before) is
format 1 plus `sample`, null unless `keys_source` is `hash_sample`: `draw_id`, `method`, `salt_label`,
`nonce`, `salt_sha256`, `drawn_at`, `draw_seq` (the `sample.draw` chain row), `population`; `targets`
(`{standard: "0.001", high: "0.0001"}`), `confidence: "0.95"`, `sided: "one-sided upper"`, `margin: "0.005"`,
`resolve` (`{times, delay_s}`, as the compiled spec holds it); `frame` (`{keys, origin:
{a, b, both}, unfit}`; member records are `attests.census`); `strata` (the draw's `{label, tier, target,
size, basis, frame_keys, drawn, contrast, u_max, x_max}`); `selection_sha256`; `ledger`, R-a's draws as
`{draw_id, spec_name, run_id, cycle, draw_seq, ended_at, status, failed_strata, closed_by, close_seq,
close_note}`, a close shown only if its seq precedes `draw_seq`; `judgement`, the cycle's
`ds.cycles.sample`. A census cycle's new requests are format 2 with `sample: null`.

`signoff_problems` appends `sample.problems(conn, run, cycle, spec)` (G1–G11 and §13's other sign-off texts), judged at request and at each
approving step from stored judgements and draws, never recomputed (`signoff_problems(…, later=False)` leaves
out G10: `pack._judged` runs G1–G9 and G11 and reads R-c from the final grant, §9). 011 replaces
`ds.approval_authors` to exclude each `closed_by` of the attested ledger (`<why>`: `you closed draw D-… of its
ledger`, after the scorer's). A sign-off's `review` adds the whole ledger, later draws with their status and
`drawn_at`, each row's `order` (`before`, `this` or `after` the signed draw), so an approver sees a newer
incomplete draw; a `sampling` spec approval's review adds its population's ledger (`order` null). §1.2's table:
§15 items 2 and 3 become checked for sampled cycles; items 4 and 5 stay the approver's. `Approvals.tsx`
accepts `attests.format >= 1` and shows the draw line, frame, strata and ledger with notes.

**Readiness** ([READINESS.md](READINESS.md) §5) reads the same judges and decides no sign-off.
`sample.problems` gains `selection: bool = True`: the readiness `sample_ledger` gate calls
`sample.problems(…, later=True, selection=False)`, which leaves out R-d's stream over the cycle's keys,
since the worker re-hashes the selection once as it measures the cycle (`sample.reselected`, stored as
the measure's `selection_ok`); `signoff_problems` and packs call it as before. R-b's texts are
`core/sampling.unpassed(judgement, draw_id, at)`, which `sample.problems` and the readiness `targets`
gate share, so a sampled cycle's failure reads the same on the Sample panel, in a refused sign-off and
on the Readiness screen.

## 11. Sampled evidence packs (PACK amendments)

### 11.1 Which cycles

Every precondition of PACK §1.1, read so: `pack._judged` accepts fresh attests of formats 1 and 2 and runs §10's
checks; (5) is a frame record of side a (`_uncounted` gains P1's `<why>` for `hash_sample`); `_facts`
refuses (P2) unless `sha256(salt_label ":" nonce)` and `selection_sha256` equal those of the cycle's
`sample.draw` row in the segment: the chain row `attests.sample.draw_seq` names, which must be action
`sample.draw` of the run naming the draw's id. The adjudication rule is unchanged.

### 11.2 What the pack adds

- `facts/attests.json` carries `sample`, bound by `attests_sha256` (step 5).
- `csv/coverage.csv` of a sampled run, and the store's export of one, gain a row per sign-off stratum (its
  members, from the stored judgement: the counts only findings give stay empty) and then a `(contrast)` row
  after `*`, and append `tier target basis frame_keys drawn contrast unresolved ub95_one_sided
  ub95_one_sided_all_suppressed ub95_one_sided_worst_case verdict` (empty on `*` and `(contrast)`;
  `absent_both` and `api_uncomparable` are columns already, filled on every row); other runs' header is
  unchanged, and a live sampled cycle has the header without rows.
- The audit segment adds the draw ids of the attested ledger (R-a's earlier draws) as objects (their
  `sample.close` rows); the run brings `sample.draw`, the sign-off its grant with `later_draws`. An
  earlier draw's own `sample.draw` row has its run as object, so the segment holds it only when that run
  is the pack's (an earlier cycle of a soak); the certificate cites any other as not in this pack (the
  SIEM holds it; step 7 does not list its run).
- The manifest gains `subject.basis: "census" | "sample"` (step 4 prints it; census packs gain it too,
  additively), `sample` (the attested `attests.sample`, as `census` is `attests.census`), and `later_draws`
  (the ledger's `bottom_k` draws after s_D at issue, as R-c weighs them: `{draw_id, spec_name, run_id, cycle,
  drawn_at, status}`, bound by the signature only); `claim_text` is §11.3's; `claim` stays `"observed"`. A
  census pack has neither `sample` nor `later_draws`.
- The certificate starts `Claim level: observed (sampled)`. "Census" becomes "Frame (observed)": side lines
  from `census.described` and `_counted`, then `Frame: 20,211,004 distinct keys (a only 111,134; b only
  106,673; both 19,993,197); 0 outside key.pattern`. New "Sample and bounds (observed)": `Draw D-… by the
  installation at 2026-10-01 09:12 UTC, in the transaction that stored the sample (audit row 81200): salt
  cutover-q4 with nonce 3f9a…(32 hex), salt SHA-256 9ab0…, selection SHA-256 5c1e…; bottom_k; strata and
  sizes approved in A-… on 2026-09-30` (the spec approval's decision date), then `To check by hand: row
  81200 of audit/chain.tsv is action sample.draw and holds both SHA-256 values, and the SHA-256 of the text
  cutover-q4:3f9a… is the salt's` (a row number as `audit/chain.tsv` and VERIFY.txt's `HEAD` write it,
  without separators), then, for keys of class `business_code` (shown in clear), `Keys are shown in clear
  here (key class business_code): with the nonce, anyone holding this pack can hash each key it lists and
  tell which of them lie in a stratum compared whole, a customer attribute.` (§19), a strata table
  (stratum, tier, target, basis, frame keys, drawn, compared, discrepant, unresolved, absent both,
  truncated, retried, bound, verdict; a census stratum's bound reads `compared whole`), the reported-only
  bounds as a line per sampled stratum (`Reported, never judged: stratum
  degree=d0's bound is 0.1001% counting every known or noise-only difference discrepant, and 0.2100%
  counting its unresolved, absent and truncated keys discrepant (the worst case)`), contrast counts
  (`Contrast keys (compared, never bounded): 12, 12 compared, 0 discrepant.`, as report.pdf says) and `No
  bound is given for the strata pooled.` New "Draws of this population (observed)": each attested earlier
  draw (`Draw D-… (spec customer-profile-2, run R-… cycle 1, audit row 12, not in this pack), before this
  one: failed stratum kind=PERSON; closed by user:U-… (audit row 13): <note>`, `audit row 12)` when that row
  is in the segment, `… not closed before it`, `…: passed`, or `No draw of this population was made before
  this one.`), `Later draws at the decision (audit row 18): D-… live` (the final grant's `later_draws`, `none` without any) and `Later draws at issue: D-… (spec
  customer-profile, run R-… cycle 1, drawn 2026-10-04 09:00 UTC): failed` (or `none`). Warnings: `SIDE B IS
  NOT IN THE FRAME: keys only side b holds were not sampled; nothing here bounds extra records of side b`;
  `12 FRAME KEYS WERE ABSENT ON BOTH SIDES: NOT JUDGED, OUTSIDE EVERY BOUND BUT THE WORST CASE` (`1 FRAME
  KEY WAS`); `2 EARLIER DRAWS OF THIS POPULATION DID NOT PASS (1 UNDER customer-profile-2): SEE DRAWS`
  (`1 EARLIER DRAW`; the parenthesis counts the draws under other names, joined by `, `, and is left out
  when every one is under this name); `1 LATER DRAW WAS INCOMPLETE OR LIVE AT THE DECISION: SEE DRAWS`
  (`2 LATER DRAWS WERE`); `DRAW D-…, MADE AFTER THIS ONE, FAILED ON 2026-10-04: SEE DRAWS`, per later draw
  failed at issue.

### 11.3 What the signature proves, sampled (certificate and `claim_text`, verbatim)

`pack.PROVES_SAMPLED`; census packs keep PACK §3.4's text. `<absent>` is the frame keys absent on both
sides, summed over the sign-off strata. Where a record of the frame's census names a `consistency` other
than `snapshot` (a MySQL view, a MySQL table outside InnoDB: [ENGINES.md](ENGINES.md) §6; a side read
from files, `fixed`: [FILES.md](FILES.md) §11), the words
`a table in one snapshot` read `a table in one pass not observed to be a snapshot`
(`pack.PROVES_SAMPLED_UNPINNED`), and nothing else changes; the frame's line and the certificate's
warning say which of the two it was (PACK §1.3). A frame whose records name no `consistency`, or
`snapshot`, keeps the text below. `ds packs verify` reads no `claim_text`, so a pack verifies with either.

"Every statement records what the DeltaSleuth installation <name> observed and stored. The signature
proves that the holder of the key with fingerprint <fp> issued these files unchanged. The audit rows are
unaltered and complete only as checked against your SIEM (step 7) or, on the host, an earlier recorded
head. Parity was observed on a sample, not a census. After a spec approval fixed the strata and sizes, the
installation drew a salt holding 128 random bits and committed its SHA-256 to the audit trail in the
transaction that stored the sample, before any key was compared; in each sampled sign-off stratum the keys
compared are the frame's keys with the smallest SHA-256 of that salt and the key, the frame being the keys
its sources held when read (a table in one snapshot, a listing as returned). Each sampled stratum's
one-sided 95% Clopper-Pearson upper bound on the share of its frame keys that such reads find discrepant
(keys a bug rule suppresses included), computed from its cleanly compared keys, is within its target; a
stratum compared whole is judged on its exact count, its unresolved keys counted as discrepant. <absent>
frame keys found absent on both sides were not judged and are outside every bound but the worst case.
Taking SHA-256 as a random function and leaving unresolved and absent keys aside, had any stratum's share
been above its target, a sign-off would have passed with at most a 5% chance: that is a property of the
procedure, not a probability that parity holds; the bounds are separate statements, not all guaranteed at
once; and a pass that follows failed draws without a real fix is weaker than 95% (the earlier draws of this
population under any spec, and the fixes recorded for them, are listed; that each fix was real is the
approvers' judgement). Nothing here proves the comparison right beyond the recorded facts (adjudication
sampled); a bound for the strata pooled; agreement for keys outside the frame (created after it was read,
held only by a side it did not list, or not returned by a listing), for keys left unresolved after retries
or absent on both sides (a worst-case bound counting them discrepant is shown), or outside the compared
population, filters, columns and time window; true times (the installation's clock); that the key was not
stolen or copied, or history not rewritten before issue by the database owner; that the pack is still
valid (check revocation); or a certified claim, which needs a frozen cut."

### 11.4 VERIFY.txt and the format

Both stay: `deltasleuth-pack/1` names the procedure filed in each site's key register, and nothing in it
depends on the basis. Step 5 binds `facts/attests.json` (salt, frame, strata, ledger, judgement), step 3 the
manifest and certificate, steps 6–7 the `sample.draw`, `sample.close` and grant rows. The one new assurance,
that the salt and selection were committed before any key was compared, is the certificate's hand check
(§11.2): the auditor compares the two values printed there with row 81200 of `audit/chain.tsv`, which step
7 anchors to the SIEM, and hashes `label:nonce` with their own tool. A /2 step would make every site re-file
for that one comparison. `ds packs verify` gains it (`sample.draw` row, both values, the salt's hash) as
its own line after step 5, from `attests.sample` (bound by step 5): `step 5 sample draw: ok (row 81200 is
draw D-…'s sample.draw row and holds both SHA-256 values; the SHA-256 of cutover-q4:3f9a… is the salt's)`,
or `FAILED (row 81200 of audit/chain.tsv is not the sample.draw row of draw D-…)`, `FAILED (row 81200 does
not hold the selection SHA-256 of facts/attests.json)`, `FAILED (the SHA-256 of cutover-q4:3f9a… is not
the salt SHA-256 row 81200 holds)`, or `FAILED (facts/attests.json's sample cannot be read)` when the file
does not parse (nested past the parser's depth, say) under a manifest of basis `sample`, or its `draw_seq`
is not an integer or its draw id, salt label, nonce or selection not text; a census pack has no such line.
The salt is hashed as `utf-8` with `surrogatepass`, and every printed line escapes a lone surrogate, so a
crafted file fails a step and exits 1, never 3. Step 4's `"basis": "sample"` goes into the change record,
and `ds packs verify` prints it (`run R-… cycle 2; basis sample; spec …`).

### 11.5 Privacy and the raw-value gate

**Per-key labels.** Only `repo.stratum_counts` (behind `sample.judge` and `progress`) and
`repo.selection_lines` (R-d's re-hash) read `run_keys.stratum`, `signoff_stratum` and `signoff_member`
(`lease_due` also returns `signoff_member` and `retries` to the worker, for §7's retries; no output
carries them).
`list_results`, `get_key_detail`, `list_keys`, every CSV (`discrepancies` and `retry` included),
`report.pdf`, the agent's tools and every API response carry labels only in aggregate: strata tables and
counts. A per-key view (a role, or `security.clear` naming `stratum`) is deferred. With keys in clear, a
draw's nonce and its strata's `u_max` would place a listed key in a stratum compared whole: routes withhold
both below rule_approver, while attests and packs keep them (§14, §19). Labelled accuracy
([ACCURACY.md](ACCURACY.md) D9, §3) reads none of them: `repo.labelling_draw` reads a cycle's results and
findings and counts its `run_keys`, never their labels or the nonce, so the reader list above is
unchanged and no labelling output of a sampled cycle carries a stratum.

**Packs.** No sampled key, per-key u or frame key enters a pack; per-key content stays a census pack's (sheet,
`key.view` rows, annex). Labels are spec constants (D5), searched in attests, manifest and CSV (the word
rule splits at `=` and `|`). The nonce is its own JSON string, 32 lowercase hex holding a letter, which
`_GENERATED` exempts; the certificate's `label:nonce` is written as label, colon and nonce, each searched
in parts. The label is spec text, searched in `facts/spec.json` (`label_key_shaped` warns early). A
bucket's `values`, `min` and `max` hold data values compared with the key or a frame column, so
`facts/spec.json`'s numbers there are searched as decimal text, as inline integer keys are (`_strings`,
the file read with exact decimals; `value_key_shaped` warns early): a watch-listed account pinned into a
bucket as `40012345` refuses the pack as `"40012345"` does. `u_max`, `x_max`, counts and bounds are
numbers or `_NUMBER` strings. Close notes, checked when written, are
searched; PACK §2.3's "never … notes" gains `(but sampling close notes, checked as revoke reasons are)`.
`_GENERATED` adds `D-` ids (`(?:[ADJPR]|LI)`). Beyond the bucket numbers, none of this needs a new rule:
the nonce is a salt of the existing digest rule, labels split at `=` and `|` as every word does, and `D-`
ids came with the closes.

## 12. Storage: migration `011_sampling.sql`

```sql
-- Sign-off sampling (docs/mvp/SAMPLING.md): a drawn key's strata, whether its own bucket chose it and its
-- fresh reads; a run's pending draw; each ended cycle's judgement; every draw, append-only but for its one
-- close. The run_keys checks are NOT VALID: every earlier row holds NULL or the default, which satisfies
-- them, so nothing scans run_keys under ACCESS EXCLUSIVE; new rows are checked.
ALTER TABLE ds.run_keys
    ADD COLUMN stratum text,
    ADD COLUMN signoff_stratum text,
    ADD COLUMN signoff_member boolean NOT NULL DEFAULT false,
    ADD COLUMN retries smallint NOT NULL DEFAULT 0,
    ADD CONSTRAINT run_keys_stratum_format
        CHECK (stratum ~ '^[A-Za-z0-9][A-Za-z0-9_.:=|+-]{0,255}$') NOT VALID,
    ADD CONSTRAINT run_keys_signoff_stratum_format
        CHECK (signoff_stratum ~ '^[A-Za-z0-9][A-Za-z0-9_.:=|+-]{0,255}$') NOT VALID,
    ADD CONSTRAINT run_keys_retries CHECK (retries BETWEEN 0 AND 10) NOT VALID,
    ADD CONSTRAINT run_keys_signoff_member_needs_stratum
        CHECK (NOT signoff_member OR signoff_stratum IS NOT NULL) NOT VALID;
ALTER TABLE ds.runs ADD COLUMN draw jsonb CHECK (jsonb_typeof(draw) = 'object');  -- {due[, error, at]}
ALTER TABLE ds.cycles ADD COLUMN sample jsonb CHECK (jsonb_typeof(sample) = 'object');  -- §8.1
CREATE TABLE ds.sample_draws (
    draw_id          text PRIMARY KEY CHECK (draw_id ~ '^D-[0-9A-Z]{26}$'),
    run_id           text NOT NULL,
    spec_id          bigint NOT NULL,
    cycle            integer NOT NULL CHECK (cycle >= 1),
    method           text NOT NULL CHECK (method IN ('bottom_k', 'rate')),
    population       text[] NOT NULL CHECK (cardinality(population) BETWEEN 1 AND 2),
    salt_label       text NOT NULL CHECK (salt_label ~ '^[A-Za-z0-9_.+-]{4,64}$'),
    nonce            text NOT NULL UNIQUE CHECK (nonce ~ '^[0-9a-f]{32}$' AND nonce ~ '[a-f]'),
    drawn_at         timestamptz NOT NULL,
    keys             integer NOT NULL CHECK (keys >= 1),
    frame            jsonb NOT NULL CHECK (jsonb_typeof(frame) = 'object'),
    strata           jsonb NOT NULL CHECK (jsonb_typeof(strata) = 'array'),
    selection_sha256 text NOT NULL CHECK (selection_sha256 ~ '^[0-9a-f]{64}$'),
    closed_by        text CHECK (closed_by ~ '^user:U-[0-9a-f]{16}$'),
    closed_at        timestamptz,
    close_note       ds.note,
    UNIQUE (run_id, cycle),
    FOREIGN KEY (run_id, spec_id) REFERENCES ds.runs (run_id, spec_id),
    CHECK ((closed_by IS NULL) = (closed_at IS NULL) AND (closed_at IS NULL) = (close_note IS NULL))
);
CREATE INDEX sample_draws_population_idx ON ds.sample_draws USING gin (population);
CREATE FUNCTION ds.tg_sample_draws() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_close text[] := ARRAY['closed_by', 'closed_at', 'close_note'];
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.drawn_at IS DISTINCT FROM now() OR NEW.closed_at IS NOT NULL
           OR NOT EXISTS (SELECT 1 FROM ds.runs r WHERE r.run_id = NEW.run_id AND r.cycle = NEW.cycle
                            AND r.status = 'running' AND r.draw IS NOT NULL)
           OR EXISTS (SELECT 1 FROM ds.run_keys k WHERE k.run_id = NEW.run_id AND k.cycle = NEW.cycle) THEN
            RAISE EXCEPTION 'ds.sample_draws: draw % is made as of now(), for a running run''s current cycle, before its keys',
                NEW.draw_id USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' OR OLD.closed_at IS NOT NULL OR NEW.closed_at IS DISTINCT FROM now()
       OR (to_jsonb(NEW) - v_close) IS DISTINCT FROM (to_jsonb(OLD) - v_close)
       OR NOT EXISTS (SELECT 1 FROM ds.cycles c WHERE (c.run_id, c.cycle) = (OLD.run_id, OLD.cycle)) THEN
        RAISE EXCEPTION 'ds.sample_draws: draw % is only ever closed, once, as of now(), after its cycle ended',
            OLD.draw_id USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sample_draws_guard BEFORE INSERT OR UPDATE OR DELETE ON ds.sample_draws
    FOR EACH ROW EXECUTE FUNCTION ds.tg_sample_draws();
CREATE TRIGGER sample_draws_no_truncate BEFORE TRUNCATE ON ds.sample_draws
    FOR EACH STATEMENT EXECUTE FUNCTION ds.append_only();
ALTER TABLE ds.approvals DROP CONSTRAINT approvals_scopes_check, ADD CONSTRAINT approvals_scopes_check
    CHECK (scopes <@ '{clear,unlisted_clear,known_diffs,population_filters,sampling,raw_clear}');
```

`ds.approval_authors` is replaced as 009 replaced it, adding `ds.accountable(l->>'closed_by')` for each `l`
in `jsonb_array_elements(coalesce(a.attests->'sample'->'ledger', '[]'))`, the scorer kept.
`repo._insert_keys` takes the label columns (from `insert_draw`, which stores the draw and then its keys: a
sampled run is created with none) and `start_next_cycle` copies them (a copy resets `retries`); `end_cycle` takes `sample`; `save_key_results` takes `fresh` (§7); `_CENSUS_PENDING` reads
`coalesce(r.params->>'keys_source', '') NOT IN ('table_scan', 'hash_sample')`; new: `draws_due`,
`insert_draw`, `set_draw`, `get_draw`, `live_sampled_runs` (R1), `schedule_retries`, `stratum_counts`,
`selection_lines`, `ledger`, `close_draws`, and for §14 `draws`, `ledger_draw`, `run_draws`, `retrying`,
`finished_since`, `newest_spec`; `lease_due` returns `signoff_member` and `retries`, and
`cycle_done` is false while a running run's `draw` is set. No trigger freezes labels: R-d re-hashes them at every
judgement. Nothing is backfilled. **Upgrade:** 011 holds ACCESS EXCLUSIVE on `run_keys` only for catalogue
changes (the new columns have constant defaults and the checks are NOT VALID): on 5M rows (802 MB) that took
1 ms, where the same columns with inline checks scanned for 0.53 s, about 5 s at 50M; `VALIDATE CONSTRAINT`
(0.3 s each at 5M, SHARE UPDATE EXCLUSIVE, so runs keep writing) may be run later, and nothing depends on
it. **Divergence from control-plane.sql (D12):** no label columns on results or findings, which share
`run_keys`' key (a stratum is an exact join), and no `attrs.origin` (origins are counts).

## 13. Messages (binding; each asserted)

**Run creation and resume** (`ds run` exits 2; `SampleRefused` in `_HANDLERS`, `_EXPECTED`):
- R1 (409) `spec customer-profile samples the population of live sampled run R-… (spec customer-profile-2,
  table:legacy-db/customers): stop it before drawing another sample (docs/mvp/SAMPLING.md §5)`
- R2 (422) `a soak over a hash sample draws a new sample each cycle: start it rolling (ds run --soak
  --rolling)`
- R3 (422) `captured samples cannot be sampled: a hash sample reads its frame from the targets`
- R4 (422) `a hash sample draws its keys from the frame: run it without --keys`
- R5 (409) `run R-… paused after draw D-… did not pass: close it with the fix (POST /v1/samples/close),
  then resume`
- APPROVALS message 1 names `sampling` among its scopes.

**Draw** (`DrawError`: the run pauses; `GET /v1/runs/{id}` shows `sample.error`; `ds run` prints it and
exits 2, `error: the run is paused`):
- W1 `the key listing of target new-api was not read completely (HTTP 200: max_pages (30000) reached before
  the last page)`; a failed page gives its own status and position, as `keys._listed` does: `(HTTP 503: page
  2: HTTP 503)`
- W2 `the key listing of target new-api lists 12 key(s) twice: its pages shifted while they were read, so
  keys may be missing (declare stable_ordering, or use cursor pagination)`
- W3 `the frame gave 12 key(s) a sign-off label naming no bucket with targetSize (degree=d7): under bottom_k
  every key needs a sign-off stratum`
- W4 `the frame gave 3 high-risk key(s) a standard sign-off label (degree=d1): test the high-risk conditions
  first in signoffStratum` (also without `signoffStratum`, for a high-risk key whose high-risk buckets have
  no `targetSize`)
- W5 `the frame gave 5 key(s) a sign-off label naming none of their buckets (kind=ORG)`
- W6 `signoffStratum failed for 5 key(s) of the frame (source 0, first at position 1,204)`; for a dimension,
  `stratum dimension degree failed for …`
- W7 `the sample would hold 1,000,001 keys, over 1,000,000: lower the rates, or compare the population as a
  census`
- W8 `the frame is empty`; `the sample is empty: raise the rates`; `the population's keys came out of order`;
  `target legacy-db is not in the registry`; `the frame of side a timed out after 3600 s (QueryCanceled)`
- W9 `no frame key falls into sign-off bucket flagged=Y: a stratum without keys certifies nothing (correct
  its values, or remove it in a new version)`

**Sign-off** (409 at request and decision, joined by `; `):
- G1 `cycle 2 of run R-… has no draw of its own: its sample was never drawn`
- G2 `the sample D-… of cycle 2 of run R-… was drawn by rate: sign-off needs bottom_k sizes fixed in the
  spec`
- G3 `the keys of cycle 2 of run R-… are not draw D-…'s selection: a sample is never extended, trimmed or
  relabelled`
- G4 `the frame of D-… holds 12 key(s) outside key.pattern or the store's limits (source 0): they could not
  be sampled; correct key.pattern in a new version and draw again`
- G5 `stratum degree=d6_20 of cycle 2 of run R-… is over its 0.1% target: its one-sided 95% upper bound is
  0.2101% (2 discrepant of 2,995 cleanly compared keys)`
- G6 `stratum flagged=Y of cycle 2 of run R-… is over its 0.01% target: compared whole, its 4,210 keys allow
  0 discrepant or unresolved key(s); it has 1`
- G7 `stratum degree=d0 of cycle 2 of run R-… has 5 unresolved key(s) after 3 retries, over the 3 its 0.1%
  target allows (of 3,006 cleanly compared keys)`
- G8 `stratum degree=d0 of cycle 2 of run R-… has 12 key(s) absent on both sides, over the 3 its 0.1% target
  allows of 3,011 drawn: the frame was stale; draw again`
- G9 (R-a) `draw D-… (spec customer-profile-2, run R-… cycle 2) failed before draw D-… was made and was not
  closed before it: close it with the fix, then sign off a draw made after the close`; `ended incomplete`
  or `ended without a judgement` for `failed` (`had not ended` for a draw still live, which D8 rules out)
- G10 (R-c) `draw D-… (spec customer-profile, run R-… cycle 5), made after draw D-…, failed stratum
  kind=PERSON on 2026-10-04: fix it, close it and sign off a draw made after the close`; for a live draw,
  `has failed stratum kind=PERSON for certain`; for an unjudged one, `ended on 2026-10-04 without a
  judgement`; several strata read `strata kind=PERSON, kind=ORG`
- R-b, a cycle ended without a judgement: `the sample D-… of cycle 2 of run R-… ended without a judgement:
  draw again`
- R-b, a cycle ended incomplete: `the sample D-… of cycle 2 of run R-… ended incomplete (stratum degree=d0
  unfinished): its cycle ended before every key was final; draw again` (`strata degree=d0, degree=d1` for
  several; without the parenthesis when every stratum is complete and only contrast keys were not final)
- a draw of the ledger (this one or another `bottom_k` draw) that no committed `sample.draw` row names: `the
  sample D-… of cycle 2 of run R-… does not match its sample.draw audit row: its order in the ledger is
  unknown`
- G11 `stratum degree=d100p of cycle 2 of run R-… has 812 key(s) above the API cap that were not compared
  (truncated): declare pagination for side b's fetch in a new version and draw again`

**Pack** (409, PACK §7.2):
- P1 `cycle 2 of run R-… is not a census: its sampling frame does not enumerate side a (the baseline); a
  frame of side b cannot show keys side b lost`
- P2 `the sample D-… of cycle 2 of run R-… does not match its sample.draw audit row: no pack`; the stale
  sign-off text names `attests format 2` too

**Close** (`POST /v1/samples/close`; checked in the order C1, C2, C5's note form, C3, C4, C5's key shape):
- C1 (409, first) `closing draws needs SSO (auth.oidc); this deployment runs in tokens mode` (or
  `developer mode`)
- C2 (403) `closing draws needs a signed-in person`
- C3 (404) `spec customer-profile not found`
- C4 (409) `the population of customer-profile has no draw to close (every ended bottom_k draw passed or is
  closed)`
- C5 (422) `the note holds a key-shaped value: write it without customer keys`; or APPROVALS' `note must be
  1 to 1000 characters without control characters`

## 14. Interfaces

| Route | Role | Answer |
|---|---|---|
| `GET /v1/runs/{id}` | viewer | adds `sample` (`{state: drawing\|comparing\|resolving\|paused, error, draw_id, drawn_at}`, `state` null once the run is no longer queued, running or paused; null for other runs) and `cycles[].sample` (`{draw, judgement}`: the draw, `withheld` as below, and the stored judgement, null for a cycle without a draw) |
| `GET /v1/samples?spec_name=&cursor=` | viewer | the name's ledger, every spec's draws (`spec_name`, `status`, `closed`, `blocking`), whole; without `spec_name`, every draw in commit order, a page at a time (keyset on the `sample.draw` chain seq); 404 `spec … not found` |
| `GET /v1/samples/{draw_id}` | viewer | the draw, `blocking` and its `ledger`; live, `progress`; ended, `precheck` (G1–G11, computed on request; null once the cycle's purge started); 404 `draw D-… not found`; `nonce` withheld while its cycle is live, and with every stratum's `u_max` from a caller below rule_approver (`samples.withheld`: with keys in clear they tell which keys lie in a stratum compared whole, §19; attests and packs keep both) |
| `POST /v1/samples/close` `{spec_name, note}` | operator person | 200 `{closed: [draw_id]}`; C1–C5 |

`progress` (`sample.progress`) is `repo.stratum_counts` on the live cycle through `sample.judged`'s verdict
rules (one query, equal to the stored judgement once the cycle ends complete, a test holds; the stored one
of an incomplete cycle also words its unfinished strata's gates, §8.2): the judgement (per
stratum drawn, n, x, `counters.outstanding`, unresolved, absent_both, `api_uncomparable` (truncated), the
verdict, `failed` once certain, and an interim bound) plus each stratum's `done` and `retrying` (retried
members not done yet), and `done`, `finished_recently` and `eta_seconds`, paced by the keys done whose
result was written in the last 15 minutes (null with none). **CLI:** `ds runs` prints `sample D-…: 15
strata, 126,453 keys` (`1 stratum`) or, while the current cycle's draw is pending, `drawing sample` (a
running run whose draw has no error), `sample to be drawn after resume` (paused) or nothing (stopped, as
`drawState` says on the Run screen); `ds run` prints `sample.lines` (`stratum degree=d0
(standard, ≤ 0.1%): 3,011 drawn of 1,204,331, 3,009 compared, 0 discrepant, 2 unresolved, bound 0.09951%:
passed`; a census stratum reads `compared whole` for `bound …`); `ds samples [--spec NAME] [--json]` (every
draw, or the name's ledger, with its status, close and `blocking`); `ds samples size` (§6); no CLI close:
`closed_by` admits only a person (`user:U-…`), whom the CLI never is. **UI:** `web/src/samples.ts` mirrors the
texts; `Run.tsx` gains a Sample panel polling `GET /v1/samples/{draw_id}` (frame, strata with progress,
interim bound, ETA, certain failures; the precheck, `blocked: draw D-… (cycle 9) failed and is not closed`
(`ended incomplete`, `ended without a judgement`); the ledger; a "Record the fix and close failed draws"
dialog); `Approvals.tsx` shows a format-2 sign-off's draw, frame, strata and ledger with notes, and a
sampling version's ledger; `packs.ts` `censusLine` reads basis `frame`. **Reports, agent:** `report.pdf`
and the P0 summary gain "Sample and bounds (observed)" (`reports/data.strata_section`, reading the cycle's
`ds.cycles.sample` for an ended cycle, whichever cycle, never recounted: the draw, the frame, the status, `No
bound is given for the strata pooled`, the contrast keys, and per stratum tier, target, basis, frame keys,
drawn, compared, discrepant, unresolved, absent both, truncated, retried and the three bounds; a live cycle's
says `Bounds are judged when the cycle ends.`, and one that ended without a judgement `Judgement: none: the
cycle ended without a judgement and cannot be signed off: draw again.`); `get_run_summary` adds `sample`, the stored judgement of the
current cycle once ended, else of the one before, which the LLM only explains.

**Readiness** ([READINESS.md](READINESS.md) §10): a sampled pair's detail (`GET
/v1/readiness/pairs/{name}`, `#/readiness/<name>`) lists the evidence cycle's `strata` from its stored
judgement: label, tier, basis, frame keys, drawn, compared, discrepant, unresolved, absent both, truncated,
verdict and why; never `weighted_rate`, a bound or `u_max`, and no rate or bound for the strata pooled
(§8.4). A sampled cycle in a pair's sparkline is a pass or fail mark, not a count. Its row of the pair's
trend (the API's, the pair screen's, `trends.csv`, `readiness.csv`, `ds readiness pair`) holds the sample's
own counts (total, compared, discrepant, unresolved, absent both, truncated, suppressed) beside its
`sample_status`, marked as the sample's where a surface words them, with no pooled rate and no bound.
A pair's cut-over brief (READINESS §10, §11) gives a sampled cycle neither `mismatch_rate` nor `coverage`
(§8.4): under `Sampled (draw D-…): judged per stratum, with no rate or bound for the strata pooled.` it
prints `sample.lines` of the stored judgement, each stratum with its own target and bound, as `ds run` does.

**Audit actions** (AUTH §6, RETENTION §8): `sample.draw` (object: the run) `{draw_id, cycle, method,
population, salt_label, salt_sha256, keys, selection_sha256, frame, strata: [{label, frame_keys, drawn}]}`,
without the nonce, so no SIEM reader can compute the sample while it is compared; `sample.draw_failed`
`{cycle, error}`; `sample.close` (object: the draw) `{spec_name, note}`; `paused` gains reason
`run_paused_sample` with `draw_id`; `approval.grant` of a sampled sign-off's final step gains `later_draws`.

## 15. Licence and retention

This build is an open build ([LICENCE.md](LICENCE.md) §19): it enforces no licence, so draws, closes,
a sampled sign-off and its pack ask for none, and no pair is counted. The rest of this paragraph's
licence rules are a vendor build's. There draws and closes work in every tier and in evaluation (D10);
sign-off stays M6, packs M7 and M4. A sampled
run is one pair, its spec name: frame targets add none, re-draws are free, and a second name over the same
population is a second pair sharing the first's ledger; a boundary paused for the licence draws after its
resume. RETENTION §4.2 gains: `run_keys` labels and retries go with the cycle; `ds.sample_draws` (salt,
nonce, population, counts, strata, close notes: aggregates) and `ds.cycles.sample` on the tombstone are never
purged, so the ledger outlives purges and erasures; the frame's temporary table goes with its session. Hold,
erasure and purge rails are unchanged; §4.3's abandon judges the cycle it ends (§5 Ends). A scheduled
sampled run is the same pair as a hand-started one of its name, and each of its draws is free once the
name is seated; `ds.schedules` holds nothing of a sample ([SCHEDULES.md](SCHEDULES.md) §7).

## 16. Performance

The 126,453-key compare takes 1.8 h at 20 req/s, concurrency ≥ 5 (§12.2); retries add at most
`resolve.times · (1 + recheck.times)` reads per never-compared member. At 20M keys: a table member streams in
its snapshot for minutes; a 20,000-page cursor listing is sequential, bound by round trips, not the budget
(about 83 min at 250 ms a page; declare `maxPages`, default 1,000); digest and heap cost 1.15 µs a key
(measured on 1M), a CEL dimension about 1.5 µs. **Dedupe, measured** on a local postgres:16-alpine
(psycopg, batches of 10,000, a union of 20M side-a keys and 20M listed keys, 19.89M in both): with
`temp_buffers = 2GB`, member 0's inserts took 188 s (9.4 µs a key) and member 1's lookups and inserts 110 s
(5.5 µs), 5.0 min in all; with the default 8 MB, 6.2 min, the table living in a temporary file that the
host's page cache happened to hold. The table took 1.8 GB (89 bytes a key, the index 0.77 GB). Member 1 now
marks the keys member 0 gave instead of looking them up (§3), which writes a new version of each such row:
at 2M + 2M keys (99.5% in both) its marks and inserts took 1.5 times the lookups and inserts (5.8 µs a key
against 3.8) and left the table 1.9 times as large (344 MB against 184 MB), so the 20M union's dedupe is
about 6 min and its table about 3.4 GB (both scaled from 2M, not measured at 20M): DEPLOY §10
leaves the database the memory `sampling.temp_buffers` names (default 2GB) and keeps a `temp_file_limit`,
if one is set, above the table. Memory holds about 20 MB of heaps, never the frame. `sample.judge` and
`progress` read one grouped query, never the cycle's rows in memory. These figures are the design's
measurements; no `DS_PERF` mark holds the build to them yet (§19).

## 17. Documents to amend

- **PACK:** Goal (`one ZIP per signed-off census or sampled cycle`) and `reports/pack.py`'s docstring; §1.1
  (5) (formats 1–2, the frame, P2); §1.2 (basis `frame`, `_CENSUS_PENDING`); §2.2 (`subject.basis`,
  `sample`, `later_draws`, certificate sections, the hand check, warnings); §2.3 (close notes, `D-` ids,
  per-key labels); §3.4 (§11.3's second text); §7.2 (P1, P2); §10 (sampled pairs built).
- **APPROVALS:** §1.1 (`sampling`, its `review.ledger`); §1.2 (format 2, `sample.problems`, the §15 table);
  §3 (closers); §6.1 (`review` ledgers); §6.2 (G1–G11, `you closed draw D-…`); §6.4 (`later_draws`), §7
  (011).
- **Others:** LICENCE §1 (D10), §7 (a second name is a second pair, one ledger); RETENTION §4.2, §4.3, §8;
  TABLES §5 (`frame_sql`); AUTH §4.1, §4.2, §6; CONTRACTS §1, §3, §4 (a sampled run's coverage export
  header); PLAN (key sources); RUNBOOK (a campaign: size, approve, soak rolling, read the precheck, close
  with the fix, request sign-off); DEPLOY §10 (`sampling.temp_buffers`, temporary space); README;
  PENTEST §2 (sample discipline, a sampled pack's draw).
- **docs/architecture.md:** §1 (sampled sign-off built); §4.3 (key sources); §6 (labels on `run_keys`); §10
  (coverage rows); §15 as built (item 2: the stored names below; item 3: in-cycle retries of never-compared
  keys, D3, D4, D13, D14, no replacement, the intersection-union note; item 4 approximated by census
  strata).
- **`spec.schema.json` (both copies):** `salt` (`the campaign label; each draw adds a nonce, so a salt
  never reproduces a sample`); `strata` (its label format is `run_keys.stratum`'s CHECK only);
  `stratumDimension.buckets` (required under `hash_sample`); `cel` (`key`,
  `now`); `hash_sample` (u keyed by `label:nonce`; labels on `run_keys` only); `union` (origins are
  counts, no `run_keys.attrs.origin`; strata from `strataColumns` or `strataFields`); new `keySourceTable`,
  `resolve`; `method` (a draw that did not pass is closed with a note, and the next draw adds a new nonce),
  `stratumBucket.rate` and `targetSize` (the census rule with its absent and truncated gates, §8.2; any size,
  `undersized` warned, `ds samples size`, no extension; only never-compared members retried, at most
  `resolve.times`).
- **`control-plane.sql`** comments: the stored names are this contract's, each saying one-sided:
  `ub95_one_sided` (exact for any x, replacing `ub95_one_sided_zero_fail`),
  `ub95_one_sided_worst_case`, `ub95_one_sided_all_suppressed`; labels on `run_keys` only.

## 18. Tests

No image is pulled (local `postgres:16-alpine`).

| File | Behaviours |
|---|---|
| `core/test_sampling.py` | `zero_failure_n` 299, 2,995, 29,956; `attempted` 3,011, 30,107; the single-stage sizes for 0 to 5 discrepant keys; **§8.3's table pinned** (`passes` verdicts; `upper` to 1e-12); oracles: a scipy `beta.ppf` grid (x 0–20, n 1–100,000), an independent `Fraction` CDF sum; `test_shown_never_contradicts` (Hypothesis); `x_max` against a linear search; census rule (5,000 → 0; 20,000 → 2); `test_false_pass_at_most_5pct` (exact P(pass \| p = t) ≤ 0.05, n 2,995–3,100, 29,956–30,000); `test_dropped_two_stage_is_0_076`; hypergeometric ≤ binomial; golden u vectors; bottom-k = `sorted(u)[:k]` whatever member order or batch size; membership, contrast, census; `seq` pace equal across strata of fractions 0.5 and 0.0001; chi-square uniformity over 1,000 salts; W4 without `signoffStratum` whichever key comes first; the bucket memo bounded over 200,000 distinct values |
| `core/spec/test_rules_sampling.py` | every §2.2 row; `key` and `now` in dimension CEL; a recency bucket; `sampling` in `needs`; a spec without a sample reading `strata` in `knownDiffs/*/when` still compiles |
| `connectors/test_paging_walk.py` | the streaming walk judges as the merged one; per-page cap; previous-page repeat |
| `runner/test_sample_frame.py` | snapshot, `skipped`, duplicate rows once, out of order refused; a listing via the mocks; union origins, first-member strata; W2 for repeats on adjacent and non-adjacent pages of either member, and for member 1 repeating a key member 0 holds, its origins counted once; a table-only frame without a temp table; unfit counted, never shown; census records; no pack key census or census-gated lease |
| `runner/test_sample_draw.py` | 202 then the task; commit order; each trigger refusal; a cancel mid-draw discards, a slow listing's read stopping at its next page; DRAW_PAUSE, resume with a new nonce; W9 for a bucket whose values match no row; rolling draws; SAMPLE_PAUSE after a failed cycle, R5 until closed; R1–R4 (R1 for a second name over the same frame); `cycle_done` while drawing |
| `runner/test_resolve.py` | a never-compared INCONCLUSIVE(timeout) and a never-compared `recheck_failed` retried, `resolve.times` at most; first read MISMATCH, rechecks unusable: `recheck_failed`, never retried, x and the worst case unchanged; DRIFT(a_changed), b_converged, truncated, absent_both never retried; a retried key's discrepancy confirmed exactly as a first read (fresh `first_outcome`, `owed`, `attrs.retried` kept); first reads counted once; contrast not retried; unsampled runs golden-equal; a key truncated again on its recheck (`recheck_failed`) fails its stratum with G11 under the default recheck policy |
| `runner/test_sample_judge.py` | counts, identities, bug-rule KNOWN_DIFF in x, every §8.2 gate and certain failure (absent_both at 0.01%, the census allowance on N_h − absent_both, G8 after retries, G11, G11 after a failed recheck; absent or truncated first reads awaiting their recheck never certain); an ended cycle's unfinished strata worded; a cycle stopped while a member waits for its retry refused (R-b incomplete); `progress` = the stored judgement with retries replacing rows; judged by `fail_run`, an erasure's end and `abandon`; `unjudged` when judging raises |
| `runner/test_sample_ledger.py` | a failed draw under name A blocks a sign-off under name B (G9) until closed, and the close covers it; C4 with only rate draws; a close racing a draw commit on two connections, judged by chain order; C5 against the named spec's and each ledger name's newest version; a ledger draw no `sample.draw` row names refused; `/list`, `/%6Cist` and `/%6cist` one population (R1, G9); R-c at request and decision, `later_draws` in the grant, a pack reading it after a later failure |
| `test_approvals.py`, `api/test_samples.py` | format 2 stable across a later draw, a close, and an earlier ledger draw's cycle purged, erased or ended by `fail_run`; formats 0, 1 still decided; G1–G11; closer excluded; `sampling` in `needs` and a `sampling` version's review ledger |
| `api/test_samples.py`, `test_label_canaries.py` | routes, C1–C5, nonce withheld while live, and with each stratum's `u_max` below rule_approver (a run's `cycles[].sample.draw` too), no route lists pending keys; no per-key label in `list_results`, key detail, keys, `discrepancies.csv`, `retry.csv`, `report.pdf`, any agent tool result or any response to a viewer |
| `reports/test_pack_sampled.py`, `test_pack_tamper.py` | files, manifest, certificate, `PROVES_SAMPLED` verbatim with `<absent>`, the hand check's values, coverage rows; census packs changed only by `basis` and format 2 (coverage header unchanged); P1, P2 (and beside another draw, never an error); an incomplete cycle refused; key-shaped label refused; side-b, absent and later-draw warnings; a pack reading `later_draws` after a later failure (here rather than in `test_sample_ledger.py`); verified by `sh` and `ds packs verify`; an edited `attests.sample` fails step 5; the draw row altered (step 6, or rewritten consistently: step 7 and the hand check) and a nonce that does not hash to the committed salt (step 7 and the hand check); a re-signed `facts/attests.json` nested past the parser's depth, or whose salt label holds a lone surrogate, fails the hand check and exits 1; a bucket's numbers (a value, `min`, `max`) refused as their text; an earlier draw of another run cited `not in this pack`; the clear-key note of a `business_code` pack |
| `reports/test_pdf.py` | a sampled cycle's report and P0 summary: live, `Bounds are judged when the cycle ends.`; ended without a judgement, `cannot be signed off: draw again` |
| `cli/test_cli.py` | `ds samples` (every draw, a name's ledger, `--json` without nonce or strata); `ds runs`: `drawing sample` for a running run only, `sample to be drawn after resume` when paused, nothing once cancelled |
| `store/test_migration_011.py` | 011 over a populated 010 store (a million `run_keys`, approvals of every scope, a scored adjudication): no rewrite or scan, constraints NOT VALID and enforced on new rows, existing approvals pass the rebuilt check, the new `approval_authors` still excludes the scorer and adds closers |
| `web/test/samples.test.ts` | the texts, `drawState` for a draw pending while paused or never made (the `DS_PERF` marks for a 1M-key frame, `judge` at 1M members and the 20M dedupe of §16 are deferred, §19) |

## 19. Limits, deferred, size

**Limits.** Every claim is observed; a listing walk is not a snapshot, keys created after the frame was read
are outside it, and a listing that silently skips keys (offset walks rely on declared `stable_ordering`)
is caught only as far as W1 and W2 reach. Strata come from frame rows (side a's row for keys both sides
hold). Of §15's high-risk strata, `flagged` and `changed in the last 7 days` (from a change-time column and
`now`) are writable; `largest 1% of components` needs a precomputed column, since rank strata are
deferred; the 12 standard strata need an entity-kind and a degree column or field. No pooled bound, 99% tier,
hypergeometric bound, hot-identifier census (§15 item 4), L2 cover, rank-based strata or `stratum` in
predicates. A stratum behind an API cap passes only once a paginated fetch covers it. A close is a
person's statement; the frame is kept only as counts; a population is named by target and table or path
(its percent-escapes canonical, §9), so a view under another name, or one database registered as two targets, is another population. A 20M-key
union draw takes about 90 minutes (the table's stream, the listing's walk and about 6 minutes of dedupe) and
about 3.4 GB of session memory or temporary space on the control plane, holding the legacy snapshot while its rows are
processed. An insider controlling a listing endpoint can omit keys, and one with database access can find
sampled keys mid-compare: reading in `seq` order and withholding the nonce narrow this, not close it. The
database owner can rewrite draws before issue, as for a census pack. With keys shown in clear (`business_code`), an
ended draw's nonce lets whoever holds it hash any key they see and, against the strata's `u_max` (or their
`drawn` and `frame_keys`, roughly), tell which of them lie in a stratum compared whole, a customer attribute
(D5): `GET /v1/samples/{draw_id}` and a run's `cycles[].sample.draw` withhold nonce and `u_max` below
rule_approver, but a sign-off's review (`GET /v1/approvals/{id}`, a viewer's) shows its attests and a pack
(a viewer downloads it) carries both, its certificate saying so.

**Deferred:** csv, sql and run_keys frames; `sampling.mode: hash`; a frame preview; per-key label views;
`ds samples verify`; `DS_PERF` marks holding a 1M-key frame, `judge` at 1M members and the 20M dedupe to
§16's figures. **Size (as built):** about 2,900 Python product lines (net), 90 of SQL, 350 TypeScript and
5,000 test lines; no new dependency.
