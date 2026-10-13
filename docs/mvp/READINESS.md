# Cut-over readiness and the multi-pair dashboard: contract

This contract is binding. It builds cut-over readiness after alerts (`012`) and labelled accuracy (`013`), on
docs/architecture.md §10 and §15, and amends the documents listed in §16.

**Goal:** per pair, grouped by programme and entity type, show from stored evidence which named gates
hold, how far each is from its target, whether parity converges across soak cycles and what is missing
before cut-over, never as a probability of parity.

**Shape:**
- The worker measures each ended cycle once onto its tombstone (`ds.cycles.readiness`).
- A pure evaluator (`core/readiness.py`) turns measures, sign-offs, packs, adjudications, labellings and
  ledgers, under gates configured in settings and floored at §15, into gate results and a ladder state.
  Every surface calls it. It imports neither the runner nor the reports: its caller hands it the two judges
  it needs from them as `Installation(mode, packs, allows, accuracy, stops)`, `reports/accuracy.gate` and
  `runner/stop.StopConditions.from_spec`.
- The worker chains each change as `readiness.changed`, which notifications route (§8).
- `deltasleuth/readiness.py` is what the API, the CLI, the reports and the worker's watch share, as
  `notifications.py` is the notifier's: `judged(ctx, conn)` runs §14's evaluation (`at=`: as of a past cycle's end,
  for a run report, §11), and the selections, a pair's detail and trend, the transitions, `check` and
  `verify` read through it. A refusal is `readiness.Refused(status,
  message)`.
- No new dependency, no outbound call; every claim stays *observed*.

## 1. Decisions (each one seam)

- **D1** Gates and a ladder (§5); the only figure is "k of m gates", never a percentage or average.
- **D2** A pair is a spec name (LICENCE §7), judged on its *current version*: the `spec_id` of its
  newest-ended evidence point (§3). Texts name versions as `1.4.0 (spec 12)`: two `spec_id`s may share a
  version string, and re-uploading reverted content reuses the older `spec_id` (`repo.save_spec` upserts
  on `content_hash`), which becomes current when a cycle of it ends.
- **D3** Only complete evidence points are evidence. Partial cycles and uncensused key lists are shown and
  break no streak, except that an unexplained CRITICAL key, or a failure certain whatever their unread keys
  show (§5 `targets`; a cycle that compared no key left none unread), fails the gate and breaks the streak:
  an observed defect counts.
- **D4** The worker measures outside `finish_cycle`, whose exceptions `_sweep_once` turns into
  `fail_run`, so readiness never fails a run. It holds `repo.get_cycle(lock=True)` against a purge's start.
- **D5** Verdicts are computed on read; `as_of` shows what was recorded then.
- **D6** Settings hold the gates, not spec bodies, which would fork trends, restart soaks and mix policy
  into approved content. A four-eyes policy table is the seam.
- **D7** §15 sets the tier: `standard` (0.1%, the default) or `high` (0.01%); a programme or pair only
  tightens. A pair observed whole is checked on one pooled count over the pair at the tier, not on §15
  item 2's per-stratum targets, which only sampled cycles are judged on and the approver checks otherwise.
  `population`, `critical` and `targets` are always required; only convergence and freshness relax, each a
  deviation named everywhere.
- **D8** Sampled cycles are judged by their stored judgement, others by SAMPLING D4's census rule, pooled.
  A key-list run given its keys at creation (`keys_source: explicit`) is evidence when its key census counts
  side a (PACK §1.2 `table`, `missing` 0); otherwise its cycles are non-evidence points (D3).
- **D9** Governance and evidence gates judge the candidate cycle (§3), always an evidence point, on stored
  facts: an approved sign-off, an issued unrevoked pack or a person-scored adjudication of it counts
  whatever the auth mode, `packs` or licence allow now. Only when none exists does a gate read `unavailable`, the auth mode, the run's start
  mode, `packs` or `licence.standing(…).allows()` saying why it cannot be made. Tokens-mode installations
  (a vendor build's Community, Pro and evaluation among them) therefore reach `parity_ready`, and key
  censuses count there once
  `readiness` is configured (§4): no fence (O2). An open build's standing allows everything, so no gate
  is `unavailable` for a licence there ([LICENCE.md](LICENCE.md) §19).
- **D10** Readiness never enters attests, packs or `VERIFY.txt`, and `signoff_problems` never reads it.
- **D11** Only the worker writes measures and readiness audit rows (`system:worker`, objects
  `readiness:<pair>` and `readiness`, outside every pack segment, PACK §4.3); `repo.end_cycle`, in whichever
  process ends a cycle, writes the excluded marker of a one-shot or captured-sample cycle.
- **D12** No forecast; §6 names only the scheduled start of the cycle that could complete a streak.
- **D13** Excluded: one-shot compares and captured-sample runs. Every batch and soak run is measured.

## 2. What readers are never told

`core/readiness.DEFINITION` heads the dashboard, pair screen, both PDFs, `ds readiness` and the API,
verbatim:

> "Readiness is a set of named gates, each met or not on observed evidence, and a place on a fixed ladder.
> It is not a probability that the two systems are at parity, not a forecast, and not sign-off evidence: a
> four-eyes sign-off decides, and its evidence pack certifies what was observed. For a pair observed whole,
> the targets gate is one pooled count over the pair; architecture §15's per-stratum targets are judged only
> for sampled cycles, and otherwise by the approver."

`test_never_told` asserts on every surface: **N1** no readiness score, gate or programme figure as a
percentage, probability, weighted score or mean (the target texts of Y4, SAMPLING's lines in Y5 and
`accuracy.gate`'s intervals in Y18 are the only `%`; a pair's brief, §10, also prints a cycle's stored
`mismatch_rate`, a counter as stored and no readiness figure, and its `sample.lines`); **N2** no forecast or
ETA: Y7's scheduled start is the only future time, always with `(a schedule, not a forecast)`; **N3**
readiness is not sign-off or evidence;
**N4** `unknown` is never met; **N5** key-list counts describe those keys only; **N6** convergence never
crosses versions, and `suppressed` sits beside discrepant counts (Y4's texts, an item's `trend`, the
dashboard's points and `dashboard.csv`, the trends); **N7** no pooled sample rate (SAMPLING
§8.4, never `weighted_rate`); **N8** "observed", never "certified"; **N9** no key (masked or raw) and no
value.

## 3. Pairs, points, cycles, bases

- **Pairs** are spec names with a batch or soak run, plus names the settings list.
- A **point** is a measured cycle. An **evidence point** is complete (§4) and not an uncensused key list
  (D8); other points are shown and skipped unless they break the streak (§6).
- **Evidence cycle:** the current version's newest evidence point.
- **Candidate cycle:** in the current streak (§6), an approved sign-off's cycle; else an open request's;
  else the newest cycle holding a passed adjudication a `user:` actor scored or a labelling a `user:` actor
  labelled; else the evidence cycle. A new cycle therefore never demotes adjudicated work still in the
  streak. The candidate is always an evidence point: a sign-off, an adjudication or a labelling of any
  other cycle (a key list given at creation without a census, a partial cycle, a captured-sample or
  one-shot cycle, D13) never makes it the candidate, and its sign-off never counts (§5).
- **Basis** (`reports/pack.census_problem`, PACK §1.2, mapped explicitly by `core/readiness.measured`):
  - `census`: side a counted in the cycle; a table scan read to its end, uncut, whose graph spec's L2 did
    not run, is `census` too (read through `census.scanned`, today's `_scanned` made public), its `graph`
    gate `unknown`;
  - `sample` (`bottom_k`) or `rate_sample`, when a hash sample's frame counts side a (the frame record is
    read only here, never as `census`);
  - `partial`: a table scan not read to its end, or cut by maxKeys (`census_problem` returns None there).
    A key list cut by maxKeys keeps the basis its census gives; its `incomplete` says `cut`;
  - `keys`, carrying the `census_problem` text; a whole table scan whose side a read files that held
    lines that were not rows is `keys` too ([FILES.md](FILES.md) §11: `runner/readiness._measured` asks
    `census_problem` for a whole scan, and `core/readiness.measured` reads a problem before the scan
    branch): a key on such a line that side b lacks is in no result, so the cycle is no census;
  - `unknown`: a purged table scan measured late, or counters older than the fields a measure reads.
- **`basis_text`** is what the texts print of the basis, worded once as the cycle is measured: for
  `census` (and a sample's frame) `census.described` of side a's record; for `keys` the `census_problem`
  text; for `partial` the sign-off's wording of the scan (`scanned only part of its tables (…)`, `was cut at
  5,000 keys (maxKeys)`), which Y14 prints; null for a purged scan. A side a read from files reads
  `read to its end from 12 fixed file(s), 3,650,722,201 bytes, pinned … and hashed the same when the read
  ended at …; an export, not the system it came from: …` ([FILES.md](FILES.md) §11): it holds no digest
  (`ds.cycles.readiness` keeps the text for ever) and never says the system the files came from.

## 4. Measures (`ds.cycles.readiness`, format 1)

```
{format: 1, pair, spec_id, version, mode, keys_source, needs_approval, complete, evidence,
 incomplete: [no_counters|cancelled|outstanding|nothing_compared|scan_partial|cut], basis, basis_text,
 total, compared, different, indeterminate, cancelled, outstanding, absent_both, api_uncomparable,
 suppressed, unexplained_critical, results, draw_id, sample_status, sample_strata: [{label, tier}] | null,
 selection_ok, changes: {new, persistent, fixed, other} | null, graph: {computed, <five codes>} | null,
 fails: {standard: [gate…], high: [gate…]}, late, measured_at}
```

About 500 bytes: counts, ids, codes, stratum labels.

- **Counts** are copied from `ds.cycles.counters` (definitions stable across versions); unresolved u =
  `indeterminate − absent_both − api_uncomparable`; `results` is `repo.count_results`. A field absent from
  an older counters shape is null, and the basis `unknown`.
- **`complete`**: own counters, `cancelled = outstanding = 0`, `compared > 0`, the scan done, no
  `keys_cut` (APPROVALS §1.2 items 2–4; `count_cycle` raises on a broken reconciliation); a table scan
  measured late is judged by its tombstone alone, its scan state being gone. **`evidence`**: complete, and
  not `keys_source: explicit` with a basis other than `census`.
- **`needs_approval`** is `approvals.needs(spec)`, so evaluation compiles no spec for `_cited`'s rule.
- **`unexplained_critical`** is the sign-off's query, `repo.unexplained_criticals(…,
  approvals.bug_rules(spec))` (public), on every point.
- **`sample_status`** is `ds.cycles.sample.status`, or `unjudged`; **`sample_strata`** each stratum's label
  and tier; **`selection_ok`** is R-d (the selection re-hashed against the draw), checked once here.
- **`changes`** (`repo.cycle_changes`, the notifier's `discrepant_changes` query extended) compares D_c, the
  DIFFERENT keys of cycle c (MISMATCH, NOT_FOUND_A/B, KNOWN_DIFF with a `bug` finding, outstanding
  included), on `results_outcome_idx`: `new` = |D_c − D_{c−1}|, `persistent` = |D_c ∩ D_{c−1}|, `fixed` =
  leavers read MATCH in c, `other` = the rest. A table scan re-reads every key not matched in c−1
  (`runner/scan.py`), so a leaver without a `run_keys` row left both tables and counts `other`. Cost: the
  discrepant keys of both cycles and, at worst, one pass over the cycle's results: the statement runs
  with `enable_nestloop` off (as `repo.labelling_draw` does), since the statistics do not know a cycle
  that has just ended and a nested loop over its results is quadratic (§14's measure test took 148 s a
  cycle with nested loops; the statement takes about half a second without). Null for first cycles, hash samples, cycles without own counters, or a previous cycle
  whose purge started.
- **`graph`** counts `ds.graph_diffs` rows by code; `computed` comes from `ds.scans.state.graph`.
- **`fails`**: the per-cycle parity gates the point fails at each tier (`standard` ⊆ `high`), fixed at
  measure time since tiers are build constants (`sampling.TARGETS`): `population` (an evidence point of
  basis `keys` or `rate_sample`), `critical` (any point with `unexplained_critical > 0`), `targets` (§5's
  rule on an evidence point; a certain failure on a partial one: a point that lacks only keys cancelled or
  owed a recheck, or that compared no key (each ended unresolved, absent or above the API cap: none is
  unread), never a partial scan, a cut key set or an uncensused key list given at creation; a failed
  judgement; for `high`, a passed judgement with a stratum below `high`), `unknown` (basis `unknown`,
  criticals null, or an evidence point `unjudged`). Graph is judged on the evidence cycle only, never here.
- **`late`**: the purge had started, so the record comes from the tombstone and criticals, results,
  changes and graph are null.

A new definition is a new `format`, and an unknown format reads `unknown`: a record whose `format` is not
this build's is an unknown point whatever its other fields say, no evidence and failing `unknown` at each
tier, so that it ends a streak as `unknown` (the series finds it through `cycles_readiness_unread_idx`) and
is told with its format (Y7, Y14). Each pair is evaluated on its own: one whose records cannot be read at
all reads `unknown` on every gate, with the note `<pair> could not be evaluated (<type>): see the server
log` and the log line `readiness: pair <pair> could not be evaluated: <type>`, and the others are judged.

**Writer.** `runner/readiness.watch(ctx, state)` runs from `_sweep_once` as one task at a time, like
`_purge`, and never raises. A pass measures a batch from `repo.unmeasured_cycles`: cycles ended since 014
first, newest first, up to 10; then, the backfill, cycles ended before 014, newest first, up to 10 (those
whose purge started, read from the tombstone alone, come with them uncounted, up to 100 cycles a batch), at
most one backfill batch every 10 s. Each batch is one
`repo.audited` transaction: per cycle, in a savepoint, `get_cycle(lock=True)` (and of the cycle before, whose
keys `changes` reads), measure, `repo.set_readiness`
(`WHERE readiness IS NULL OR readiness ? 'error'`); then `readiness.measured {cycles: [[run_id, cycle,
sha256_hex(jsonio.dumps(record, sort_keys=True))], …]}` last (AUTH §6). Then it evaluates (§8), whatever
the batches met: a pass whose measuring raised logs `readiness pass failed: <type>` and still evaluates.

A failed measure stores `{format: 1, pair, spec_id, error: "<type>", attempts, fails: {standard: [unknown],
high: [unknown]}, measured_at}`, logs `readiness: cycle 3 of run R-… not measured: <type>` and is retried
after 1, 2, 4 and 8 hours; the fifth failure adds `final: true`, an `unknown` point for good. Whatever a
measure raises is such a failure, a statement the server cancelled or refused among them (`QueryCanceled`
under a `statement_timeout`, the 53 and 54 classes, a deadlock): its savepoint is rolled back and the rest
of the batch is stored. Only a lost connection (`repo.lost`: the connection is broken, or the error has no
SQLSTATE or one of class 08, as the notifier's feed tells it) ends the batch, which the next pass measures. Excluded
cycles get `{format: 1, excluded: one_shot|captured_samples}` from 014 and from `repo.end_cycle`.

**Key censuses.** The worker counts them with `packs` or `readiness` configured (`readiness: {}` suffices):
`self.counting = settings.packs is not None or settings.readiness is not None`. As with packs today,
`repo.lease_due(…, census=True)` then leases no key of a key-list run with a table side until its cycle's
census is recorded, or recorded failed: each such cycle starts comparing after one snapshot read of each
table side (an hour a statement at most, PACK §1.3). Scans and hash samples are unaffected.

## 5. Gates, states, score

A gate item is `{gate, group, required, status, value, allowed, over, run_id, cycle, text}`; status is
`met`, `not_met`, `unknown`, `not_applicable` or `unavailable`; `text` is §9's; allowances are
`core/sampling.share(t, n) = ⌊t·n⌋`.

| Gate | Group | Required | Met when | Otherwise |
|---|---|---|---|---|
| `population` | parity | always | evidence basis `census` or `sample` | `not_met` (`keys`, `rate_sample`); `unknown` |
| `critical` | parity | always | `unexplained_critical = 0` on the evidence cycle, and no newer point of its version fails `critical` | `not_met`; `unknown` when null |
| `targets` | parity | always | not sampled, pooled: x + u ≤ share(t, N − ab), ab ≤ share(t, N), api = 0 (x `different`, u unresolved, ab `absent_both`, api `api_uncomparable`, N `total`); sampled: `sample_status = passed` and every stratum's tier at or above the policy's; and no newer point of its version fails `targets` | `not_met`; `unknown` when unjudged |
| `convergence` | parity | always | streak ≥ K (§6) | `unknown` when the streak ends below K at an `unknown` or unmeasured point and such points, were they clean, could complete it (§6); else `not_met` |
| `fresh` | parity | always | the evidence cycle ended within `max_age` of the database's `now()` | `not_met` |
| `graph` | parity | `gates.graph.required` (default no) | no GROUP_MEMBERS_DIFFER, CLUSTER_SPLIT, CLUSTER_MERGE or CLUSTER_REARRANGE row in the evidence cycle | `unknown` without L2, and for a cycle measured late; `not_applicable` for a spec without `graph` |
| `spec_approval` | governance | yes | the approval a sign-off would cite (`approvals._cited`'s rule, `needs_approval` from the measure) is approved | `unavailable` for a run started outside SSO |
| `adjudication` | governance | yes | the candidate's newest adjudication a `user:` actor scored (PACK §5) passed | `not_applicable` below `adjudicate.EXIT_MINIMUM` results; `unavailable` when none is stored and the mode is not oidc |
| `sample_ledger` | governance | yes | sampled candidate: `sample.problems(…, later=True, selection=False)` is empty and `selection_ok` | `not_applicable` when not sampled; `unknown` until every parity gate is met (the ledger is read only then, §14), unless `selection_ok` is false |
| `accuracy` | governance | when `gates.accuracy` is set | `reports/accuracy.gate` (§9 Y18) over the candidate's labelling: the one its open or approved sign-off attests, else the newest a person labelled, else the newest labelled | `not_applicable` when not configured, or the candidate has fewer than `min_decided` discrepant keys |
| `signoff` | evidence | yes | an approved, unrevoked sign-off of the candidate (stored) | `unavailable` when none exists and the mode is not oidc, the run started outside SSO, or `allows.signoff` is false |
| `pack` | evidence | yes | an issued, unrevoked pack of the signed cycle (stored; a purged ZIP counts) | `unavailable` when none exists and `packs` is absent or `allows.packs` false |

- `sample.problems` gains `selection: bool = True`; the gate skips R-d's stream (`selection_ok` holds it,
  re-hashed by the new `sample.reselected`), and its text lists the problems `targets` does not already
  show (Y5): R-b's texts are `core/sampling.unpassed(judgement, draw_id, at)`, which `sample.problems` and
  the `targets` gate share.
- **Accuracy**: labelled accuracy labels discrepant items, so a candidate with fewer than `min_decided` discrepant keys
  (x + outstanding) reads `not_applicable`; a labelling whose uniform items are every discrepant item (a
  census) is judged on all its decided items, `min_decided` aside, but never on none: one that decided
  nothing is `not_met`; a labelling with `discrepant: null` labels none, so it is treated as no labelling.
- **Sign-off**: only a sign-off of an evidence cycle counts. An approved one of another cycle of the
  current version, ended since the streak began, reads `not_met` with its own text (Y12), and counts
  neither for `signed_off` nor for `regressed`.
- **No evidence cycle.** Nothing was observed to judge: the parity gates, `spec_approval` and
  `adjudication` read `unknown` (the last two `unavailable` outside oidc mode), `sample_ledger` and
  `accuracy` `not_applicable`, `signoff` and `pack` `not_met` or `unavailable` (or what the next bullet
  gives them while the licence standing cannot be read), each with Y14's text.
- **Licence standing.** When `licence.standing` raises, `signoff` and `pack` of a pair without the stored
  fact keep what the licence last decided of them: a `not_met` or `unavailable` in the pair's newest
  `readiness.changed` row, with the text `the licence standing could not be read (see the server log): last
  known`. Otherwise (no row, or a row that says `met`, which only a stored fact gives: a sign-off revoked
  since) they read `unknown`, with `the licence standing could not be read (see the server log)`: never
  `met`. A pair without an evidence cycle keeps Y14's text on both. An unread standing is never the cause
  `licence`: a gate that moves to or from `unknown` names its own cause (§8).

Only required gates enter the ladder and the score; the others are shown and never emit. The score is
`{passed, required}` over required gates, `unavailable` and `not_applicable` ones left out:

| Installation | Required gates counted (a census pair, plus `sample_ledger` when sampled, `graph` and `accuracy` when required) | m |
|---|---|---|
| tokens or developer mode, any licence | population, critical, targets, convergence, fresh | 5 |
| oidc, Enterprise, `packs` | the five, spec_approval, adjudication (from 50 results), signoff, pack | 9 |
| oidc, Enterprise, no `packs` | the same but `pack` | 8 |
| oidc, a licence without sign-off (after the O3 grace, a downgrade) | the same but `signoff` and `pack` | 7 |

A stored sign-off or pack counts in every row. An open build, which needs no licence, is the first row
in tokens or developer mode and an Enterprise row under oidc, by `packs`; the last row is a vendor
build's alone.

**States** (lowest first):

| State | When (required gates) |
|---|---|
| `no_evidence` | there is no evidence point |
| `not_ready` | a parity gate is `not_met` |
| `unknown` | otherwise, a parity gate is `unknown` |
| `parity_ready` | every parity gate is met or `not_applicable`, and no higher rung holds |
| `signoff_ready` | also every governance gate is met or `not_applicable`, and `signoff` is `not_met` (an open request shows `awaiting A-…`) |
| `signed_off` | parity is met and `signoff` is met |
| `evidenced` | `pack` is met too |

An `unavailable` governance gate or `signoff` leaves the pair at `parity_ready`, and an `unavailable` `pack`
at `signed_off`, each with its text. `regressed` flags a pair whose current version holds an approved,
unrevoked sign-off of an evidence cycle while the pair is below `signed_off`. A programme is its worst open pair, with `k of n
pairs parity-ready or better`; entity rows count states (`[{entity, states: {<state>: n}}]`, the pairs without
an entity type last: JSON has no null key).

## 6. Series, convergence, trend

**Streak** (`core/readiness.streak`). Its *boundary* is the newest of: the newest point of the pair
failing at the policy's tier (`fails.<tier>` non-empty, any version, evidence or not), the newest evidence
point of another version, and the oldest unmeasured cycle of the pair's runs. The streak k counts the
current version's evidence points after the boundary, up to W = `max(60, 3K)`; it ends as `unknown` when the
boundary is an `unknown` or unmeasured point. A cycle (a sign-off's, or one holding a person's work) is *in
the streak* when it is an evidence point of the current version ended after the boundary: one comparison,
whatever its age.

Behind such a boundary the series still gives what certainly ended the streak: the newest of the K newest
failing points that fails a gate other than `unknown`, or another version's evidence. When the current
version's evidence points, the unknown points and the unmeasured cycles since then number fewer than K, no
measure of them could complete the streak: convergence is `not_met`, naming the failed cycle and the point
the streak reaches (Y7). Otherwise it is `unknown`.

**Earliest** (k < K, a live soak of the current version, running or paused): cycle N = `run.cycle + (K −
k) − 1`, the run's current cycle counting as the next. `finish_cycle` advances `run.cycle` as the previous
cycle ends and the new one waits for its due time, so N is running now only when it is the run's current
cycle and `cycle.due(spec, run, N) ≤ now`; otherwise it is scheduled to start at `max(cycle.due(spec, run,
N), now)`. It completes the streak only when it ends complete. When
`StopConditions.from_spec(body).at_boundary(c, run.started_at, max(cycle.due(spec, run, c + 1), now))`
gives a reason for some c in `run.cycle … N − 1`, the soak stops after c first; a paused run gives its reason (`manual` without one). Of several live
soaks, the earliest N counts: one running now, else the first scheduled start, else a soak that stops, else
a paused one. The evaluator computes `cycle.due` from the stored `execution.cycleInterval` and calls
`StopConditions.from_spec` as `Installation.stops` (no compile).

**Scheduled runs** ([SCHEDULES.md](SCHEDULES.md) §7) are measured like any batch or soak run (D13):
nothing is built for them. Scheduled batch runs of one version are evidence points, build the streak
across runs and keep `fresh` (`test_scheduled_runs_are_evidence_points`). The streak never crosses
versions, so it restarts when a following schedule takes a new version or a pinned one is moved to it.
*Earliest* reads live soaks only: a schedule's next due time is not in Y7's tail (§15).

**Trend.** Points carry version, basis, evidence, counts, criticals, changes, sample status, graph rows and
`fails`, marking skipped points and version boundaries (`core/readiness.trend`); a sampled cycle's counts
are its sample's own, beside its `sample_status`, with no pooled rate and no bound (SAMPLING §8.4), and the
surfaces say so; `bucket=day|week` (UTC
days, ISO weeks) takes each bucket's last evidence point, never an average; concurrent runs interleave by
`ended_at`. An item's `trend` (`core/readiness.spark`) is the pair's last 10 evidence points of any
version, so that a failing pair shows the cycles that failed.

## 7. Configuration (`deltasleuth/config.py`)

```yaml
readiness:                          # optional; present (even {}), key censuses are counted (§4)
  gates:                            # the installation's; unset values take this contract's defaults
    tier: standard                  # standard (0.1%) | high (0.01%): architecture §15's tiers
    convergence: {cycles: 3}        # 1..50; fewer relaxes
    fresh: {max_age: 7d}            # 1h..90d; longer relaxes
    graph: {required: false}
    accuracy: {min_decided: 50, max_b_wrong: 0}      # labelled accuracy; absent: not required
  programmes:
    - name: cutover-q4
      gates: {convergence: {cycles: 5}}
      pairs:
        - {spec: customer-profile, entity: customer}
        - {spec: customer-links, entity: customer, gates: {tier: high}}
        - {spec: legacy-accounts, entity: account, closed: 2026-11-02}   # judged as of that day, then silent
```

- **Defaults:** the tier is §15's; K = 3, `max_age` 7 days and graph not required are this contract's, added
  to architecture §15 (§16).
- **Types:** closed frozen dataclasses like `RetentionSettings` (a pair's `entity`, `gates` and `closed`
  are optional; `accuracy.min_decided` is 1 to 1,000 and `max_b_wrong` 0 to 1,000);
  `core/readiness.policy(settings.readiness, name)` merges build, installation, programme and pair values
  into `Policy(gates, programme, entity, closed, sha256, deviations)`, `gates` being `Gates(tier, cycles,
  max_age, graph, accuracy)`. `sha256` is `core/readiness.config_sha256(settings.readiness)`: one hash for
  the installation, of the block as judged (`effective`: the installation's gates and each listed pair's,
  defaults filled in), so an absent block and `{}` hash alike.
- **Deviations** name each non-default value, on every surface: `tier high (default standard)`,
  `convergence 1 cycle (default 3; relaxed)`, `convergence 5 cycles (default 3)`, `fresh 14d (default 7d;
  relaxed)`, `graph required (default not required)`, `accuracy 50 decided, 0 where side b was wrong
  (default not required)`.
- **When settings apply:** at start. The worker's first evaluation audits `readiness.config` when the
  effective block differs from the newest such row (the `licence.watch` pattern), in the transaction of the
  `readiness.changed` rows it writes and before them: they alone name `config` (§8). A server mismatch
  shows Y16.
- **`ds readiness check`** prints every pair's effective gates tagged `(default)`, `(installation)`,
  `(programme)` or `(pair)`, the hash, deviations and warnings (`programme cutover-q4 names spec
  custmer-profile, which is not stored`).

## 8. Transitions and events (the notification seam)

**When.** While any cycle ended before 014 is unmeasured or holds a non-final error (`repo.backfill_owed`,
one index probe), `watch` only measures and writes no `readiness.changed`. Afterwards it evaluates all pairs through the API's functions when:
- it measured a cycle;
- a relevant row entered the audit chain, at least `READINESS_MIN_S = 30` s after the last evaluation. It
  follows `ds.audit_chain.seq` (commit order under the DSAU lock, as the notifier's feed does) from a cursor held in
  memory, set to the head at start: `SELECT max(c.seq) FROM ds.audit_chain c JOIN ds.audit_log l ON l.id =
  c.id WHERE c.seq > %s AND l.action = ANY(%s)`, over `run.cycle`, `run.finished`,
  `approval.request|grant|reject|revoke`, `pack.issue|revoke`, `sample.draw|close`, `adjudication.score`,
  `accuracy.label` and `licence.state|install|withdraw` (`repo.readiness_entered` also gives the chain's
  head: the cursor moves to the head once nothing relevant is left behind it, so rows that change no pair
  are read once). The cursor only tells that an evaluation is due, never why a pair changed: a process's
  first pass evaluates whatever entered before it, and a row that commits while an evaluation reads is
  still behind the cursor, so the next one is due for it;
- `READINESS_EVERY_S = 900` s have passed.

A measured cycle is judged at once, a failed measure too (its point is `unknown`). The first pass of a
process evaluates: a restart writes nothing when nothing changed. An evaluation that raises (a statement
the server cancelled, say) writes nothing, logs `readiness evaluation failed: <type>` and is tried again
`READINESS_MIN_S` later, not every pass; the passes between still measure.

**What it writes.** Each pair is compared with its newest `readiness.changed` (`repo.last_readiness`, a
LATERAL on `audit_log_object_idx`). A pair with none gets one with `from: null`, cause `baseline` (the whole
installation once the backfill ends, a new pair later). When the state, `regressed` or a required gate's
status differs, one `repo.audited` transaction writes:

```
readiness.changed  object readiness:<pair>  by system:worker
{pair, programme, entity, from, to, was_regressed, regressed, score: {passed, required}, causes,
 gates: {<gate>: <status>}, changed: [{gate, from, to, value, allowed, over}],
 evidence: {run_id, cycle} | null, candidate: {run_id, cycle} | null, signoff_id, pack_id, config_sha256}
```

- `gates` holds the required gates only, so a gate that becomes required, or no longer is, differs too: its
  `changed` entry has `from` or `to` null. A baseline's `changed` is empty. `was_regressed` is the newest
  row's `regressed` (false for a baseline), which tells the notifier that it turned true.
- **Causes** say why, told for each pair from what differs between its newest row and the pair as
  evaluated (`core/readiness.causes`), never from the rows that entered the chain meanwhile: neither a
  restart, nor a row committed while an evaluation reads, nor another pair's row names a pair's cause. A
  row names each that holds, in this order:
  - `baseline`: the pair's first row, alone;
  - `cycle`: the evidence cycle is another, or `population`, `critical`, `targets`, `convergence` or `graph`
    changed on the same one (a newer point that is no evidence, or that could not be measured, moved it);
  - `version`: the current version changed (the spec of the newest row's evidence cycle, which
    `repo.last_readiness` reads with the row, is no longer current: both have one);
  - `signoff`: `signoff` or `spec_approval` changed; `pack`, `adjudication` and `accuracy`: that gate
    changed; `ledger`: `sample_ledger` changed;
  - `licence`, instead of the gate's own cause: `signoff` or `pack` moved between `unavailable` and `not_met`
    while the candidate's run stayed, so what the licence allows changed (or the deployment's `packs`);
  - `config`: the row was written by the first evaluation under newly recorded settings, in the
    transaction of their `readiness.config` row and after it. No later row names `config`: a pair the
    settings did not move keeps its row, and its next change names its own causes. In that evaluation a
    parity gate that changed on the same evidence cycle is the settings' alone (neither `cycle` nor
    `time`). While the backfill withholds the evaluation the settings are recorded alone, and no row names
    them;
  - `closed`: the pair is judged as of its day;
  - `time`: `fresh` changed on the same evidence cycle. It is also the one cause of a row that names no
    other, but that a row which changed `regressed` alone names `signoff` (an approval, or its revoke, of a
    cycle outside the streak).
- **Closed pairs.** Once its day has passed, a pair is compared, as judged at the end of that day, with its
  newest row while that row was written by then, and a difference is chained with cause `closed`; after a
  row written later (that one, or a baseline) it is silent, whatever is recorded or observed. Judged as of
  a past time, it is told no live soak's schedule (Y7's clause is left out, as in a run report, §11): the
  soaks and their cycles are today's.
- **Noise rules (binding):** nothing during the backfill; nothing of a pair one of whose cycles was never
  measured (the pass that measures the cycle judges the pair, its baseline included: a pass measures ten
  cycles and others end while it does, and an unmeasured cycle would read `unknown` for a second; a closed
  pair's cycles ended after its day do not hold it); partial and non-evidence points flip only `critical`
  and `targets`; gates that are not required never emit; one row per pair per change; closed pairs stay
  silent after their day.
- **Content and timing.** Only ids, states, counts and slugs; without notifications the SIEM line notifies. Rows land
  within about 30 s of a committed cause, 15 minutes for `time`.

**Notifications (ALERTS amendments, §16).**
- **§2.1 row** `readiness.changed` | from `readiness.changed` | facts below | severity: `info` for
  `baseline`; `alert` when `regressed` turned true (`regressed` and not `was_regressed`), or a changed gate
  went `not_met` or `unknown` from `met`, or went so as the state fell; `warning` when a gate went
  `unavailable` or the state fell otherwise; else `info` (so a gate that became judged, from `unavailable`,
  `not_applicable` or not required, without the state falling is no alert: a licence renewed) | dedup key
  `readiness.changed|<pair>|<to>|<mask>`, `<mask>` the required gates `not_met` or `unknown` as a 3-digit
  hex bitmask in `core/readiness.GATES` order, the first gate the highest bit (at most 102 characters). A
  delivery is a duplicate only of the pair's newest pending or sent delivery to the channel
  (`core/notify.superseded`): a fall that follows a delivered recovery is sent.
- **§2.3 fact model** `ReadinessChanged` (`extra="forbid"`): `pair`, `programme`, `entity` as slugs
  (`^[a-z][a-z0-9]*(-[a-z0-9]+)*$`, ≤ 63, Z8's pattern) and labels (§6.4: null and `withheld` without
  `labels`, `pair` also when `rawvalues.key_shaped` flags it); `from` a state `Literal` or null; `to` a state;
  `was_regressed`, `regressed` bool; `score` two integers 0–12; `causes` a list of cause `Literal`s; `gates`
  a map of gate to status `Literal`s (the required gates: the dedup mask is taken from it); `changed` ≤ 12
  `{gate, from, to}` of `Literal`s (null for a gate not required then), never a gate's counts; `evidence`,
  `candidate` `{run_id, cycle}` or null; `signoff_id`; `pack_id` (the id pattern gains `P`); `config_sha256`
  (`^[0-9a-f]{64}$`, a new pattern). The model checks the slugs; a channel's labels are withheld as its
  payload is made (the pair's newest stored version tells whether its name is key-shaped; a name only the
  settings list is kept), and a withheld pair's link is `#/readiness`.
- **§3 rule keys**, for `readiness.changed` only but `specs` and `states`: `specs` globs match the pair;
  `programmes` (fnmatchcase globs; a pair in no programme matches only rules without it); `states` the `to`
  state; `gates` (a listed gate is in `changed`); `causes`, required (the rule fires when any cause it lists
  is among the row's: a `baseline` row, or one that names `config` alone, fires only a rule that lists it,
  and no row is dropped for the causes it names besides). `core/notify.fires` checks each present key. A
  rule that names `readiness.changed` names no other event, and `states` takes a licence state or a
  readiness state for either kind.
- **§6.2 wording:** `[prod] cutover-q4 / customer-profile: readiness not ready -> parity ready (5 of 5 gates;
  targets met, convergence met; cycle 14 of run R-…). Observed; readiness is not a sign-off.` (`a pair` when
  withheld). A baseline reads `readiness first recorded as parity ready`, a row whose state stayed
  `readiness still not ready`; `regressed` joins the parenthesis when the pair is; the notification's footer follows.
  ALERTS §6.2 bans the word parity in a notification: the state's name `parity ready` is its one exception.
- **§10 messages:** the reservation's line goes; `notify.rules.0: Value error, readiness.changed needs causes
  (baseline and config are never implied)`; `notify.rules.0: Value error, programmes, gates and causes are
  only for readiness.changed`; `notify.rules.0.states.1: expected a licence state or a readiness state`;
  `states is only for licence.state and readiness.changed`.

## 9. Messages (binding; each asserted)

`…` stands for `cycle 14 of run R-…`; versions print as `1.4.0 (spec 12)`. The sign-off's wording is reused
where the condition is the same. Every gate × status has a text:

- **Y1** (population, met) `… observed side a whole (<census.described>)` (a side a read from files:
  `(read to its end from 12 fixed file(s), …; an export, not the system it came from: …)`, which is met
  only while no line of its files was not a row: [FILES.md](FILES.md) §11); `… compared bottom_k sample D-…,
  its frame reading side a whole`.
- **Y2** (population, not met) `pack.census_problem`'s text verbatim (among them `… is not a census: side
  a's files hold 3 line(s) that were not rows`, C1 of [FILES.md](FILES.md) §15), plus `: its counts
  describe those keys only` (a census pending adds ` (side a is counted only with packs or readiness configured, PACK §1.3)`);
  `… compared rate sample D-…, which monitors: draw bottom_k sizes to judge a population`. Unknown: `…
  ended before readiness was measured (014) and its scan state was purged: its population is not known`;
  `… holds counters from before the fields readiness reads: its population is not known`.
- **Y3** (critical) met `… has no key with unexplained CRITICAL findings`; not met `… has 3 key(s) with
  unexplained CRITICAL findings (bug rules count as unexplained); the gate allows 0`, `cycle 15 of run R-…,
  which is not evidence (partial | a key list whose census does not count side a), has 2 key(s) with
  unexplained CRITICAL findings (bug rules count as unexplained): an observed defect counts`; unknown `…
  ended before readiness was measured (014) and its findings were purged: not measured`.
- **Y4** (targets, not sampled) met `… has 12 discrepant or unresolved key(s) of 1,000,000 observed whole,
  within the 999 its pooled 0.1% target allows (per-stratum targets are the approver's); 4 key(s) suppressed
  (known differences or noise only)`; not met `… has 1,204 discrepant or unresolved key(s) of 1,000,000
  observed whole; its pooled 0.1% target allows 999: 205 over; 4 key(s) suppressed (known differences or
  noise only)` (`of 5,000 listed keys` for a list); `… has 12 key(s) absent on both sides, over the 5 its
  0.1% target allows of 5,004: the key source is stale`; `… has 812 key(s) above the API cap that were not
  compared (truncated): declare pagination for that side's fetch in a new version`; `partial cycle 15 of run
  R-… already has 1,204 discrepant or unresolved key(s) of 1,000,000, over the 999 its pooled 0.1% target
  allows whatever its unread keys show; 4 key(s) suppressed (known differences or noise only)`, and of a
  cycle that compared no key `cycle 15 of run R-…, which compared no key, has 1,000,000 discrepant or
  unresolved key(s) of 1,000,000, over the 999 its pooled 0.1% target allows; 0 key(s) suppressed (…)`
  (absent and API forms likewise, after `partial … already` or `…, which compared no key,`). The suppressed
  count follows every discrepant count (N6), 0 included. Several unmet parts are joined by `; `. Unknown:
  `… holds counters from before the fields readiness reads: not measured`.
- **Y5** (targets, sampled) met `sample D-… of … passed every stratum at its tier`; not met the judgement's
  `why` lines verbatim (SAMPLING G5–G8, G11), its R-b texts, or `stratum high-risk of … was judged at
  standard (0.1%); the pair's policy is high (0.01%)`; unknown `the sample D-… of … ended without a
  judgement`.
- **Y6** (graph) met `… has no CLUSTER_SPLIT, CLUSTER_MERGE, CLUSTER_REARRANGE or GROUP_MEMBERS_DIFFER row`;
  not met `… has 3 CLUSTER_SPLIT, 1 CLUSTER_MERGE, 0 CLUSTER_REARRANGE and 12 GROUP_MEMBERS_DIFFER row(s);
  the gate allows 0`; unknown `graph parity (L2) did not run in …`, `… ended before readiness was measured
  (014) and its graph rows were purged: not measured`; not applicable `1.4.0 (spec 12) declares no graph`.
- **Y7** (convergence) met `the newest 5 evidence cycles of 1.4.0 (spec 12) meet every per-cycle gate: at
  least the 3 the gate needs`; not met `1 of the 3 evidence cycles in a row that 1.4.0 (spec 12) needs meet
  every per-cycle gate (cycle 13 of run R-… failed targets)`, then `; cycle 16 of run R-… is the earliest
  that can complete them: it is scheduled to start about 2026-10-07 14:00 UTC, cycles starting every 1h, and
  completes them when it ends complete (a schedule, not a forecast)` (or `: it is running now`, of a run's
  current cycle once it is due), `; run R-… stops after cycle 15 (maxCycles) before it can complete them`
  (`until`, `maxDuration`), `; run R-… is paused: <reason>` or `; no live soak of 1.4.0 (spec 12) will add
  cycles`; unknown `the streak of 1.4.0 (spec 12) reaches cycle 11 of run R-…, which is not measured yet`
  (`which could not be measured (<type>)`, `whose measures are of format 2, which this version does not
  read`, `whose findings were purged before 014`, `whose counters predate the fields readiness reads`,
  `whose sample ended without a judgement`). Not met where such points cannot complete the streak (§6):
  `0 of the 3 evidence cycles in a row that 1.4.0 (spec 12) needs meet every per-cycle gate (cycle 13 of
  run R-… failed targets; the streak reaches cycle 15 of run R-…, which could not be measured (<type>))`.
  Of the parenthesis, the failed cycle is left out when the streak ends at another version's evidence or
  the pair has too few cycles, and the point reached when the streak ends at none. When the point
  reached is not measured yet, or its failed measure is still tried again, no schedule clause follows: what
  it comes to read decides which cycle is the earliest.
- **Y8** (fresh) met `the evidence cycle (14 of run R-…) ended 2 days ago, within the gate's 7 days`; not
  met `… ended 9 days ago, over the gate's 7 days`. The age is in whole days, under a day in whole hours;
  the gate's span is the setting spelled out (`36h` is `36 hours`).
- **Y9** (spec approval) met `… ran under spec approval A-…, approved`; not met `… ran without a spec
  approval`, `… ran under spec approval A-…, since revoked`; unavailable `run R-… started in tokens mode:
  only a run started under SSO is signed off`.
- **Y10** (adjudication) met `adjudication J-… of cycle 14, scored by a signed-in person, passed` (here
  and in Y12 and Y19, `cycle 14 of run R-…` when the cycle's run is not the evidence cycle's); not met
  `adjudication J-… of cycle 14 did not pass: <score.exit.reasons joined by '; '>`, `no adjudication of …
  is scored: draw and score one before requesting sign-off, which freezes the cycle's adjudications
  (docs/mvp/PACK.md §5)`; not applicable `… has 11 per-key results, under the 50 an adjudication needs`;
  unavailable `adjudication needs SSO (auth.oidc); this deployment runs in tokens mode` (or `developer
  mode`).
- **Y11** (ledger) met `draw D-… of … meets the draw discipline (SAMPLING §9)`; not met each
  `sample.problems` text `targets` does not show, such as `draw D-… (spec customer-profile-2, run R-… cycle
  2) did not pass before draw D-… was made and was not closed before it: close it with the fix, then sign
  off a draw made after the close`, or R-d's `the keys of … are not draw D-…'s selection: a sample is never
  extended, trimmed or relabelled`; unknown `the draw discipline of draw D-… of … is read once every parity
  gate is met`; not applicable `… is not sampled`.
- **Y12** (sign-off) met `sign-off A-… of cycle 14 approved on 2026-10-03`; not met `request sign-off of
  cycle 14 of run R-… on the Run screen, the newest cycle in the streak whose adjudication passed (or the
  newest evidence cycle): every other gate is met` (only in `signoff_ready`), `sign-off A-… requested for
  cycle 14 awaits a rule_approver`, `cycle 12 was signed off (A-…), but cycle 14 fails targets: the
  sign-off is outside the streak`, `cycle 4 of 1.4.0 (spec 12) was signed off (A-…); 1.5.0 (spec 15) is
  current: sign off a cycle of it`, `sign-off A-… was revoked on 2026-10-03 (see Approvals)` (the
  candidate's own), else `… is not signed off: the gates before it are not all met`; the first that
  applies, in the order: an open request, `signoff_ready`, a sign-off outside the streak (`but cycle 14 ran
  another version`, or `is not measured`, for a boundary that is no failed gate), the candidate's revoked
  one, another version's. Without an open request, an approved sign-off of a cycle that is no evidence
  (§5) is told instead: `cycle 1 of run R-… was signed off (A-…), but is not evidence (a key list whose
  census does not count side a): only a sign-off of an evidence cycle counts` (`partial`, `not measured`),
  followed in `signoff_ready` by `; request sign-off of …`; unavailable
  `sign-off needs SSO (auth.oidc); this deployment runs in tokens mode`, Y9's unavailable text, and in a
  vendor build `sign-off
  needs an Enterprise licence, which this installation's does not allow (docs/mvp/LICENCE.md §6)`.
- **Y13** (pack) met `pack P-… issued 2026-10-02 for sign-off A-…`; not met `sign-off A-… has no issued
  evidence pack: issue one on the Run screen`, `pack P-… was revoked on 2026-10-04: issue a new one`, `no
  sign-off to pack yet`; unavailable `evidence packs are not configured (packs)`, and in a vendor build
  `evidence packs need an
  Enterprise licence, which this installation's does not allow`.
- **Y14** (no evidence; every parity gate `unknown` with it) `no batch or soak cycle of customer-profile has
  ended`; `12 cycle(s) ended, none evidence; the newest: <reason>`, the reasons APPROVALS §1.2's: `… has no
  counters of its own (failed, erased or abandoned)`, `… is partial: 3 key(s) cancelled, 0 owed a recheck`
  (cancelled, outstanding), `… compared no key`, `… scanned only part of its tables (rows_a 812,000, rows_b
  811,950 read; the scan stopped before the end)`, `… was cut at 5,000 keys (maxKeys)`, `… could not be
  measured (<type>)`, `… holds measures of format 2, which this version does not read`, or `… is a key list
  given at creation whose census does not count side a` (with ` (side a is counted only with packs or
  readiness configured, PACK §1.3)` while that census is pending, as Y2); `no spec named customer-profile is
  stored`; `2 ended cycle(s) are not measured yet` for a pair whose only cycles are not measured. Both
  counts are read up to 100: more read `over 100 cycle(s) ended, …`.
- **Y15** `2 ended cycle(s) are not measured yet` (`; 1,200 ended before 014 are being measured, and no
  transition is written until they are`: the first count is of cycles never measured, the second also of
  those ended before 014 whose failed measure is tried again; each is read up to 100,000, more reading
  `over 100,000`); **Y16** `this server judges with readiness settings <sha12>, the
  worker with <sha12>: restart both after changing them`; **Y17** `closed on 2026-11-02: judged as of that
  day`; **Y19** `regressed: 1.4.0 (spec 12) was signed off (A-…, cycle 12) and the pair is now below signed
  off`; the licence texts of §5; the note of a pair that could not be evaluated (§4).
- **Y18** (accuracy) `reports/accuracy.gate(row, *, at, min_decided, max_b_wrong, signoff=None)` returns
  the status and its lines (the gate's text joins them by line breaks). `row` is the candidate's labelling
  as `repo.readiness_governance` gives it, not its score alone: a labelling's score holds neither actor nor time,
  and the labelling line needs them (its ids, the actor that labelled, its dates, its truth source, its
  sides' roles and its `accuracy`; no person's name is read); None without one, `at` naming the cycle; `signoff` is the sign-off attesting
  it. The lines: `labelling V-… of cycle 14 meets (or: does not meet) the accuracy gate, which allows 0
  decided items where side b was wrong and needs 50 decided (all, for a census of the discrepant items)`, then
  `accuracy.lines`' labelling line without its people (`Labelling V-… drawn 2026-10-02, labelled 2026-10-04
  by a person against <truth source> as of 2026-10-01, attested by sign-off A-….`: `through a token`, `in
  developer mode` or `by no signed-in person` for another actor, never a name, a token or a host account)
  and its side-b share verbatim (population, n, interval). Not met also `no labelling of … labels its
  discrepant items: draw and label one (Labelled accuracy, Run screen)`, `labelling V-… of cycle 14 decided
  40 items, under the 50 the gate needs` (`decided 0 items, under the 1 the gate needs` for a census that
  decided nothing); not applicable
  `… has 12 discrepant key(s), under the 50 the accuracy gate judges` (`no discrepant key`), `no accuracy
  gate is configured`.

**A pair's brief** (§10), in `core/readiness`'s words:

- **M1** (`BRIEF`, after `DEFINITION`) `This brief restates stored measures of one pair's newest complete
  cycle and last 30 measured cycles: counts, stored counters and per-stratum bounds, observed. It adds no
  score, says nothing of the future, attests nothing and decides nothing.` **M7** (`LEGEND`) `Marks: V a
  new spec version; K another key source or key-list size. Hollow: an incomplete cycle. A sampled cycle
  has no rate and draws its own keys. A purged cycle keeps no root-cause groups.`
- **M2** (no reported cycle) `No batch or soak cycle of <pair> is measured yet.`, or `None of the newest 12
  measured cycle(s) of <pair> is complete; the newest: <Y14's reason>`. **M3** `Cycle 3 of run R-… is
  purged: its root-cause groups are not kept.` **M6** `Root-cause groups over 12 complete cycle(s) since
  2026-10-01: 2 opened in the newest, 5 still open, 9 closed (not observed in it)`; then, where a group is
  closed and the reported cycle did not compare every key (a cycle is complete with keys INCONCLUSIVE or
  in DRIFT, and a root cause groups compared keys only), `; 7 key(s) of the newest were not compared, so a
  closed group's keys may be among them`; then `; the first 500 of 1,204 are listed` when cut.
- Of the reported cycle, in this order: **M8** (the pair's evidence cycle is another one) `The gates judge
  cycle 14 of run R-…; these figures are of cycle 2 of run R-…, the newest complete cycle.`; **M4**
  `Sampled (draw D-…): judged per stratum, with no rate or bound for the strata pooled.` (unjudged, Y5's
  `the sample D-… of … ended without a judgement`); **M5** (basis `keys`, `rate_sample`, `unknown`) `These
  counts are of the keys compared, not of a population.`; **M9** (not sampled, its counters holding a
  rate) `Mismatch rate 0.075: 3 different of 40 compared. Known differences and noise-only keys count as
  matching; a known difference under a bug rule counts as different.`
- A cycle's counters round `mismatch_rate` and `coverage` to six places. The brief's points hold them as
  stored; where one is printed beside its counts (M9, `Compared`'s coverage, the trend's rate) a stored
  `0` of a count above none reads `under 0.000001`, and a stored `1` of a count short of its whole `over
  0.999999` (`core/readiness.ratio`, `ready.ratio`).

**API errors** (each on every route that takes the parameter): 404 `pair customer-x not found: no spec or
readiness setting names it`; 404 `programme cutover-q9 is not configured`; 422 `state must be one of <the seven states>`; 422 `gate must be one of <the
twelve gates>`; 422 `bucket must be cycle, day or week`; 422 `closed must be open, closed or all`; 422
`limit must be 1 to 500`; 422 `as_of must be an ISO-8601 time with a UTC offset`; 422 `trends.csv would hold
2,200,000 rows, over its 100,000: narrow it with programme=, entity=, pair= or bucket=day`; 422 `volume must
be 1 to 5 for this selection (100 pairs a volume)`; 400 `malformed cursor`; 404 `report 'x.pdf' not found`;
422 `brief.pdf is a pair's report: give pair=` (so `brief.csv` and `causes.csv`; `programme` and `entity`
are ignored for the three); 422 `causes.csv would hold 120,400 rows, over its 100,000`.
`limit` is judged on the list and on the transitions; `bucket` and `volume` on every report, though only
`trends.csv` is bucketed and only `dashboard.pdf` has volumes.

**CLI errors** (exit 2, one `error:` line): the 404 texts; `cycle 3 of run R-… does not hash to its
readiness.measured row (seq 81234): its measures were altered after they were written` (of a cycle a row
names that holds other measures, an error record, an excluded marker, a record without a pair or none);
`cycle 3 of run R-… is named by 2 readiness.measured rows: its measures were removed and measured again`;
`cycle 3 of run R-… has measures no readiness.measured row names`; `cycle 3 of run R-… holds a record that
is neither measures nor an excluded marker`; `cycle 3 of run R-… is marked excluded (one_shot) but its run
is a soak`; `cycle 3 of run R-… is marked excluded (captured_samples) but its run read no captured samples`;
`state must be one of <the seven states>` for `--fail-unless`; `--pair and --programme cannot be given
together` for `report`.

**Settings errors** (`invalid settings <path>: <loc>: <msg>`, exit 2 at every entry point): Z1 `spec
customer-profile is in programmes cutover-q4 and cutover-q1: a pair belongs to one programme`; Z2 `programme
cutover-q4 is named twice`; Z3 `spec customer-profile is listed twice`; Z4 `expected standard or high:
readiness targets never go below architecture §15's 0.1%`; Z5 `standard is looser than programme
cutover-q4's high: a programme or pair only tightens targets` (`than the installation's high` under an
installation tier); Z6 `expected 1 to 50`; Z7 `expected a
duration from 1h to 90d`; Z8 `expected a lower-case kebab-case name of 1 to 63 characters, as a spec name`.
`<loc>` is `readiness` for Z1, Z2 and a tier looser than the installation's, `readiness.programmes.<n>` for
Z3 and Z5, and the setting itself for Z4 and Z6 to Z8 (`readiness.gates.tier`).

## 10. Interfaces

**API** (`api/routes/readiness.py`, mounted before `runs`; role `viewer`; only downloads are audited):

| Route | Answer |
|---|---|
| `GET /v1/readiness?programme=&entity=&state=&gate=&closed=open\|closed\|all&q=&as_of=&limit=&cursor=` | `{definition, now, as_of, config_sha256, worker_config_sha256, notes, programmes, entities, items, next_cursor, volumes}`, worst first (then by name); `closed` defaults to `open`; `limit` 1–500 (100); `volumes` is how many `dashboard.pdf` volumes the pairs of the programme and entity type make, closed ones too (§11) |
| `GET /v1/readiness/pairs/{name}?precheck=1` | the pair as listed, with `definition`, `now`, both settings hashes, `candidate`, `convergence {streak, needed, window}`, `notes`, `points` (the streak's, W at most), `versions` (each one's newest evidence cycle), `runs` (the live soaks of the current version as they stand now, a closed pair's too); the evidence cycle's `strata` (counts and verdicts, never `weighted_rate` or a bound); labelled accuracy's `labelling` (its id, cycle, time and the sign-off attesting it: its lines are the accuracy gate's text); with `precheck=1` only, `precheck`: `{label: "computed now; the sign-off decides", run_id, cycle, purged, problems}`, the `approvals.signoff_problems` of an unsigned candidate in oidc mode (`purged` on `CyclePurged`), else null; with `packs` configured its `problems` end with PACK §7.2's line for a spec whose key pattern cannot tell a key (`rawvalues.no_pack`: `spec 12's key.pattern matches the empty string: a raw key cannot be told from other text, so no pack`), which refuses no sign-off and every pack of the spec |
| `GET /v1/readiness/pairs/{name}/cycles?bucket=&cursor=` | `{pair, bucket, items, next_cursor}`: the trend, 500 points a page, oldest first, the pages going back in time; keyset on `(ended_at, run_id, cycle)` (by `day` or `week`, the bucket before) |
| `GET /v1/readiness/pairs/{name}/brief` | the pair's cut-over brief: the pair as listed (Items; `trend` unchanged) with `definition`, `brief` (M1), `now`, `config_sha256`, `notes` (the detail's, then M2 or M3), `legend` (M7), `points` (the window, oldest first: each the trend's row plus, as stored, `mismatch_rate` and `coverage`, null when sampled, SAMPLING §8.4, or `unavailable`; `by_outcome`, `cancelled`, `outstanding`, `root_causes`, its count of groups, null once purging; and `marks`), `cycle` (the reported point with `draw_id`, `claim: "observed"`, `strata`, its `sample.lines`, and `notes`: M8, M4, M5, M9 as they apply; null without one) and `causes: {opened, still_open, closed, total, cycles, from, text, items}` (`text`: M6; an item: `{root_cause_id, state, first, last, seen, max_severity, taxonomy_code, change_type, affected_keys, since}`, `first` and `last` a `{run_id, cycle, ended_at}`; null without a reported cycle, or when it is purged); computed on read (D5) with the evaluation in one `REPEATABLE READ, READ ONLY` transaction, so a purge between its reads changes nothing; it writes nothing |
| `GET /v1/readiness/transitions?pair=&programme=&limit=&cursor=` | `readiness.changed` rows (`{seq, at, detail}`), newest first; `limit` 1–500 (100) |
| `GET /v1/readiness/reports/{name}?programme=&entity=&pair=&bucket=&volume=` | `dashboard.pdf`, `dashboard.csv` or `trends.csv` (§11), audited as `report.download {name, programme, config_sha256}` on object `readiness`; a later volume is named, and audited as, `dashboard-<n>.pdf`; `bucket` and `volume` are judged for each report (§9). With `pair`, also `brief.pdf`, `brief.csv` and `causes.csv` (§11), whose row gains `pair` and holds `programme: null`, whatever was asked |

- **Items:** `{pair, programme, entity, version, spec_id, state, regressed, score, gates, blocking,
  evidence: {run_id, cycle, ended_at, basis, claim: "observed"}, trend, since, signoff, pack, closed,
  deviations}`. `gates` are §5's items; `blocking` is the required gates not met, lowest rung first;
  `signoff` and `pack` are the approved sign-off's and the issued pack's ids; `since` the time the pair
  entered its current state: the `at` of its newest `readiness.changed` row that changed the state (rows
  since may have changed a gate only), when its newest row's `to` is the current state, null until the
  worker has recorded it;
  `trend` the last 10 evidence points, oldest first:
  `[{run_id, cycle, ended_at, spec_id, sampled, value, allowed, suppressed, failed: [gate]}]` (`value` x + u,
  `allowed` the tier's allowance and `suppressed` the cycle's suppressed keys, all null when sampled).
- **`as_of=T`** answers each pair's newest `readiness.changed` at or before T, as items holding only `{pair,
  programme, entity, state (to), regressed, score, gates (statuses), evidence: {run_id, cycle}, candidate,
  signoff_id, pack_id, config_sha256, since (when it entered that state, as above), seq}`; `closed` is not
  applied, as a row does not say.
- **Filters.** `gate` takes the pairs whose required gate is `not_met` or `unknown`; `q` those whose name
  holds it. `programmes` and `entities` roll up the selection before `state` and `gate` narrow it. `notes`
  are Y15 and Y16. The list's cursor is `<state's rank>.<pair>`, a trend's `<microseconds>.<run>.<cycle>`,
  the transitions' an audit row id.
- **Brief.** The window is the pair's newest `BRIEF_CYCLES = 30` measured points (a closed pair's up to
  its day); the reported cycle is the newest `complete` one (§4) whatever its basis, so a `--keys` run
  shows, under M8 and M5. Marks: `version` is the trend's `boundary`; `keys` says that, against the
  nearest earlier point holding measures, `keys_source` differs, or both are `explicit` (keys given with
  the run) or both `table` (a key table, read again at each run and each roll) and `total` differs. Over
  the window's complete points up to the reported one whose root causes are kept, a group
  is `opened` (in the reported cycle, in no earlier one), `still_open` (in both) or `closed` (earlier
  only: not observed in it, which is not "fixed"), `since` holding the marks after its `last` cycle
  (`closed, keys changed since`); an incomplete point neither opens nor closes. `BRIEF_CAUSES = 500` are
  listed, open first; never `path_pattern`, `sample_keys` or `member_key_tok` (N9).
- Readiness never answers 410.

**CLI** (`readiness_app`; reads unaudited): `ds readiness [--programme P] [--entity E] [--state S] [--as-of
T] [--json] [--fail-unless STATE]` prints `cutover-q4: 2 of 5 pairs parity-ready or better (worst: not
ready)`, then per pair `customer-profile 1.4.0 (spec 12)  customer  not ready  6 of 9 gates  cycle 14 of R-…
(2026-10-03)` and the unmet gates' texts, under `DEFINITION`, the settings hash and Y15, Y16; `--fail-unless`
exits 1 when a listed pair is below STATE. Also `ds
readiness pair NAME [--bucket B] [--precheck] [--json]` (each point `2 different, 0 unresolved, 0
suppressed of 1,000`, ` in its sample` for a sampled cycle), `report --out DIR [--programme P]` (every
`dashboard-<n>.pdf` volume, `dashboard.csv`, and `trends.csv`, or `trends-<pair>.csv` per pair beyond
100,000 rows; `report.download` per file; with `--pair NAME` instead that pair's `brief-<pair>.pdf`,
`brief-<pair>.csv` and `causes-<pair>.csv`, whole, each audited with `pair`), `check` and `verify` (§12).

**UI** (`web/src/Readiness.tsx`, pure `ready.ts`, `api.ts` types, a nav link for every role; no chart
library. The pure module is not `readiness.ts`: beside `Readiness.tsx`, an import of either resolves to
the other on a file system that ignores case):
- **`#/readiness`** shows the definition, the settings hash, Y15 and Y16. Per programme it gives `k of n
  pairs ready` and a pairs-ready step chart from the last 90 days of `transitions` (≤ 500 rows, read once
  for every programme; the chart counts the pairs listed, walking back from their states now, a pair not
  recorded yet not counted as ready; with more than 500 rows it begins where they do). Per pair it
  gives the state (text and shape), `6 of 9` with a cell per gate, an inline-SVG sparkline of `trend`
  (value against allowance, its title naming each cycle's suppressed count too; sampled points as pass or
  fail marks), the evidence cycle, since, sign-off, pack and deviations. Filters sit in the hash query; a button for each
  `dashboard.pdf` volume (the answer's `volumes`: `dashboard.pdf 2 of 5`, saved as
  `readiness-dashboard-2.pdf`; one button, `dashboard.pdf`, for a selection of one volume) and one for each
  CSV download the reports of §11; the page polls every 60 s, under `X-DS-Background` once the tab is idle
  (AUTH §3).
- **`#/readiness/<name>`** is read-only: the ladder, gates by group, convergence, the trend with version
  markers (a sampled cycle's counts marked as its sample's own, with the line `A sampled cycle's counts are
  its sample's own: no rate and no bound is given for the strata pooled.`), changes, strata, live runs,
  transitions and a "Check sign-off now" button (`precheck=1`).
- **`#/readiness/<name>/brief`** (`ReadinessBrief`, reached from the pair's screen and the Run screen) is
  the brief in §11's five parts. Figures, strata and gates link to `#/runs/<run>?cycle=` (an outcome
  figure with `&outcome=`, a group to its last run with `&root_cause_id=`), versions to `#/specs/<spec_id>`
  (the stored spec, read-only), the sign-off to `#/approvals/<id>`. The trend is an inline SVG
  (`ready.briefChart`): two strips on one x axis, the stored mismatch rate above the root-cause groups,
  each from 0 to its own printed maximum; a linked dot a point (hollow: incomplete; none for null), a
  vertical line lettered `V` or `K` a mark. 10 groups show, the rest under `All groups`; a button a report.
- **`Run.tsx`** adds `Pair readiness: not ready (6 of 9 gates)`, a link to the pair, then `cut-over
  brief`, on a batch or soak run (nothing for a one-shot run, or a name readiness does not judge), and
  lists `readiness.csv` among its reports.

## 11. Reports and exports

- **`report.pdf`** gains "Pair readiness (observed)" after `_trend` (`reports/readiness.run_section`):
  `DEFINITION`, then the parity gates as of the report's cycle (`readiness.judged(…, at=ended_at)`: `now =
  ended_at`, points and governance up to it) in a table of gate, required, status and text, under the line
  `The parity gates of <pair> as of cycle 14 of run R-… (ended 2026-10-03 04:00 UTC), judged on the cycles
  ended by then under readiness settings <sha12>: the database gives them again under the same settings
  (architecture §10).`, the pair's deviations and `Today's state: the Readiness screen
  (#/readiness/<name>).`; else `Readiness is judged when the cycle ends.`, `This cycle is not measured
  yet.` or `This run is not assessed for readiness: <one-shot | captured samples>.` (told from the run's
  mode and `samples_dir`, as `end_cycle` marks its cycles). The convergence text leaves out Y7's schedule
  clause there: the live soaks and their current cycle are today's, not the reported cycle's, so naming
  an earliest cycle beside a past streak would be wrong, and the section would change with them.
- **`dashboard.pdf`** (`reports/readiness.dashboard_document(ctx, volume, **filters)`, a `render.Document`
  for `render_pdf`): at most 100 pairs, worst first; a selection of more is split into volumes, the header
  saying `volume 2 of 5 (pairs 101–200 of 500)`, never truncated; closed pairs are in it, with Y17. It holds a header (installation, time,
  settings hash, deviations, the licence state, `DEFINITION`), counts per programme and entity, per pair the
  gates, convergence, sign-off, pack and last 10 evidence points (`12 discrepant or unresolved key(s), 999
  allowed, 4 suppressed`), and "What this report is not" (N1–N8).
- **`brief.pdf`** (`reports/readiness.brief_document`, of the brief as the API answers it; `pair=` only),
  one landscape A4 page under `dashboard.pdf`'s header line: (1) the state, `DEFINITION`, M1, notes and
  deviations; (2) the newest complete cycle: M8; cycle, ended, version, basis, `observed`; `Keys
  attempted`, `Compared` (with `coverage`), `Match`, `Known difference`, `Noise only`, `Mismatch`, `Not
  found on a`, `Not found on b` (these six add up to `Compared`), `Inconclusive`, `Drift`, `Unexplained
  CRITICAL`, `Root-cause groups`; M9, or M4 and the strata lines; M5 (without a cycle, M2); (3) `k of m
  gates`, each gate's status, and the texts of the blocking gates, `spec approval`, `sign-off` and `pack`;
  (4) the trend's points with their marks, and M7; (5) M6 or M3, and the groups. `Document(compact=True)`
  sets 7pt lines inside `KeepInFrame(mode="shrink")`: content over the page is scaled down, never split.
  It prints 4 strata, the first 3 blocking gates' texts (one over 300 characters cut with `…` where its
  last clause by then ends, so that Y7's schedule stands with its label or not at all, N2), the newest 5
  points and the first 6 groups, a line saying how many more there are and where (`… 1,198 more: causes.csv`).
- **CSVs** (`reports/csv.render_csv`, OWASP-escaped):
  - `dashboard.csv`, one row per pair × gate (≤ 6,000 rows at 500 pairs): `programme entity pair version
    spec_id state regressed gates_passed gates_required gate required status value allowed over suppressed
    run_id cycle ended_at basis deviations config_sha256 text` (`suppressed`: on the `targets` row, beside
    the discrepant count its `value` is);
  - `trends.csv`, one row per pair × point, at most 100,000 rows (counted first on the index; more is
    refused, §9): `programme entity pair version spec_id run_id cycle ended_at basis complete evidence total
    compared different unresolved absent_both api_uncomparable suppressed unexplained_critical new
    persistent fixed other sample_status graph_rows fails`;
  - `reports.FORMATS["readiness.csv"]`, `(ctx, run_id, cycle)` (`reports/readiness.run_csv`): `trends.csv`'s
    rows of the run's pair up to and with that cycle, the newest 500 at most, oldest first (every point
    while the cycle is live), which the run report route, `ds report --kind` and `report.download` serve
    unchanged. It reads measures only, so it is served for a purged cycle too;
  - `brief.csv`, one row per point of a pair's brief (`pair`, then the point's fields: `by_outcome` as
    eight lower-case columns, `fails` and `marks` space-joined), and `causes.csv`, every group of it,
    never cut (refused over `CAUSES_ROWS = 100,000`, §9; `first` and `last` as three columns each).

## 12. Roles, audit, verification, licence, retention, privacy

- **A side read from files** ([FILES.md](FILES.md) §13): a measure's `basis_text` holds the count and
  bytes of its files and their times, never a file's name and never a digest (`files_sha256` is in no
  readiness row, answer or report).
- **Roles:** viewers read everything; nothing writes readiness (the settings are the host administrator's).
  Transitions and `as_of` are projections of worker rows only (ids, states, counts, slugs; no actor,
  address or request data), so reading them is not `audit.read` (AUTH §4.1 amendment).
- **Audit** (AUTH §6, RETENTION §8), by `system:worker`: `readiness.changed`; `readiness.measured` (object
  `readiness`, ≤ 100 cycles a row); `readiness.config` (object `I-…`, `{from_sha256, sha256, gates,
  programmes, deviations}`: `gates` and `programmes` are `effective`'s, `deviations` `[{pair, deviations}]`
  for the installation's gates, `pair` null, and each listed pair with any). `report.download` may also take object `readiness`.
- **`ds readiness verify`** checks the stored measures against the `readiness.measured` rows, both ways
  (the rows are the audit chain's, which `ds audit verify` checks). Every cycle a row names must be named
  by one row only and still hold a record with `pair` and no `error` that hashes to it; every record with
  `pair` and no `error` must be named by a row; an excluded marker must agree with its run (`one_shot`
  mode, or `samples_dir`); a record with neither `pair` nor `excluded` is refused (014's CHECK does not
  store one). The first that fails exits 2 (§9). It prints `readiness: 12 measured cycle(s) match their
  readiness.measured rows; 3 excluded, 1 not measured`, the last being the error records, which no row
  names.
  - **Detected:** the measures of a cycle a row names edited; removed (the column emptied, or the
    tombstone deleted); replaced by an error record, a record without a pair or an excluded marker;
    removed and measured again by the worker (two rows then name the cycle: the worker stores a cycle's
    measures once); measures no row names; an excluded marker on a batch or soak cycle that read no
    captured samples.
  - **Not detected:** what is done to a cycle before its measures are audited. Its counters, results and
    findings edited before the worker measures it are measured as they stand: verify checks measures
    against their audit rows, never against what they were measured from. An error record put on a cycle
    no row names passes (error records are not audited): the cycle is an `unknown` point, which never
    reads met, and unless the record says `final` the worker measures it again.
- **Licence:** no fence. The routes join `test_roles.ROUTES`, which `test_nothing_locks_out` walks. In an
  open build the reports' header prints `licence state open` (LICENCE §19).
- **Table pre-checks and the schema guard** ([PRECHECKS.md](PRECHECKS.md) §9): no readiness module
  reads `ds.prechecks`, `ds.precheck_pairs` or `ds.runs.guard`, and a pre-check is no point, cycle or
  evidence of any gate (`test_precheck_is_never_evidence` holds the import graph, and that no readiness
  module's source names a pre-check's tables, their repo functions or `guard`). A run the schema guard
  paused is a paused run to Y7, which already prints its reason (`run_paused_schema_drift`).
- **Retention:** the column survives purge and erasure with the tombstone, like `sample`.
- **Privacy:** counts, ids, spec names, versions and slugs only, and in the accuracy gate's text the truth
  source and dates its labelling names (Y18); no person, token or host account (the evaluation reads no
  actor's name); stratum labels in aggregate, never in audit rows or events; no nonce, `u_max` or key.
  The population gate's text holds more than that. It
  names the relation a key-list run's table side was counted against (`census.described`) and, of a
  cycle that is no census, a table side's relation or the target id its key listing reads
  (`pack.census_problem`). A pair's gates as readiness words them, on the Readiness screen and in `ds
  readiness pair`, hold those names; the MCP server serves a pair's gates without their texts for that
  reason (MCP §7; its canary tests hold both cases).

## 13. Migration `014_readiness.sql`

```sql
-- Cut-over readiness (docs/mvp/READINESS.md §4): each ended cycle's measures, written once by the worker
-- and kept with the tombstone; one-shot and captured-sample cycles are marked excluded here and by end_cycle.
ALTER TABLE ds.cycles ADD COLUMN readiness jsonb
    CHECK (jsonb_typeof(readiness) = 'object' AND readiness ? 'format'
           AND readiness ?| ARRAY['pair', 'excluded']);
UPDATE ds.cycles c SET readiness = jsonb_build_object('format', 1, 'excluded',
        CASE WHEN r.mode = 'one_shot' THEN 'one_shot' ELSE 'captured_samples' END)
FROM ds.runs r
WHERE r.run_id = c.run_id AND (r.mode = 'one_shot' OR r.params->>'samples_dir' IS NOT NULL);
CREATE INDEX cycles_unmeasured_idx ON ds.cycles (ended_at DESC)
    WHERE readiness IS NULL OR (readiness ? 'error' AND NOT readiness ? 'final');
CREATE INDEX cycles_unmeasured_run_idx ON ds.cycles (run_id, cycle) WHERE readiness IS NULL;
CREATE INDEX cycles_readiness_pair_idx ON ds.cycles ((readiness->>'pair'), ended_at DESC)
    WHERE readiness ? 'pair';
CREATE INDEX cycles_readiness_unread_idx ON ds.cycles ((readiness->>'pair'), ended_at DESC)
    WHERE readiness ? 'pair' AND readiness->'format' <> '1';
CREATE INDEX cycles_readiness_evidence_idx
    ON ds.cycles ((readiness->>'pair'), (readiness->>'spec_id'), ended_at DESC)
    WHERE readiness @> '{"evidence": true, "format": 1}';
CREATE INDEX cycles_readiness_breaks_standard_idx ON ds.cycles ((readiness->>'pair'), ended_at DESC)
    WHERE readiness->'fails'->'standard' <> '[]';
CREATE INDEX cycles_readiness_breaks_high_idx ON ds.cycles ((readiness->>'pair'), ended_at DESC)
    WHERE readiness->'fails'->'high' <> '[]';
```

No trigger and no rewrite; results, findings and `run_keys` are untouched; `control-plane.sql` gains the
column and indexes. New repo functions: `unmeasured_cycles` (the worker's batches), `backfill_owed` (whether
a cycle ended before 014 is still to measure: one probe), `unmeasured_pairs` (the evaluation's unmeasured
cycles: of the installation, how many and how many the backfill still owes, each up to 100,000; per pair,
how many, up to 100, and the oldest),
`set_readiness`, `cycle_changes`,
`graph_counts`, `readiness_pairs`, `readiness_series`, `readiness_governance`, `last_readiness` (with `at`
for `as_of`, each row's evidence cycle's `spec_id`, and `since`, when its state was entered), `readiness_entered` (§8's chain read),
`readiness_transitions`, `readiness_points` and `count_points` (a pair's trend, and `trends.csv`'s count),
`readiness_measured` (`verify`'s stream: the cycles that hold a record and those a `readiness.measured` row
names), and `lost` (whether an error is a lost connection, which the notifier's feed uses too). `repo.end_cycle` writes the column, so a test that ends a
cycle on a store before 014 writes its tombstone itself.

## 14. Performance

Targets held by `DS_PERF` tests (`tests/unit/runner/test_readiness_perf.py`), not measurements (the
benchmark harness, BENCHMARK.md, does not time them).

- **`readiness_series`**, one statement, a LATERAL per pair: the newest evidence point of each version (a
  skip scan of `cycles_readiness_evidence_idx`, tens of versions), the K newest breaks at the tier (one probe
  of a `breaks` index, and one of `cycles_readiness_unread_idx` for records of another format), the newest
  `critical` and the newest `targets` break of that version after the evidence cycle (a probe each), the
  current version's evidence points after the newer of the newest of those breaks that fails a gate other
  than `unknown` and the other versions' evidence, up to W (≤ 150 entries; `core/readiness.streak` then
  applies the unknown breaks and the oldest unmeasured cycle), the pair's last 10 evidence points (the
  trend: the last 10 of each of its 10 newest versions on the evidence index, merged) and, for a pair
  without evidence, its newest point (one probe of `cycles_readiness_pair_idx`) and how many it has, counted
  up to 100 and one more on the same index: no read grows with a pair's history, and each cycle found is
  then read by its key. It takes each pair's tier, K, W and the end it is judged up to (a
  closed pair's day, a report's cycle). A streak only counts its points, so each comes without its measures
  unless a pair's detail lists them (`whole=True`): at K = 50 they are 150 records a pair. The planner
  prices the statement's per-pair index probes high enough to compile it (`jit`), which took two thirds
  of its time at 500 pairs, so it turns `jit` off for the rest of the caller's transaction.
- **`readiness_governance`**, one statement over the candidates, evidence points all: sign-offs and
  requests in the streak, person-scored adjudications and labellings in it (a labelling's `accuracy` folded in,
  without its `by_cause` and `by_rule`, with what its labelling line prints, no actor's name), cited
  approvals, packs
  and live soaks with their spec bodies' `execution` and stop conditions (no compile); a candidate's draw is
  named by its measures. It gives the cycles the candidate is chosen from (§3), `core/readiness.candidate`
  choosing, and each sign-off with whether its cycle is an evidence point (and the measures of one that is
  not, for Y12).
- **Evaluation** (dashboard, watch): 7 statements whatever the history: pairs, series, the unmeasured
  cycles (`unmeasured_pairs`: their two capped counts, then per pair), governance, newest rows per pair,
  `licence.standing`; plus, per sampled pair whose parity gates are met, `ctx.spec` and `sample.problems`'
  three reads (and one `judge` per live later draw). The unmeasured cycles ended since 014 are few and
  read whole; while a backfill is owed, each run of the pairs is probed instead
  (`cycles_unmeasured_run_idx`: its oldest unmeasured cycle, and its count up to 100), so that the read
  does not grow with what the backfill owes.

| Operation | Target |
|---|---|
| Measure a cycle: its discrepant keys and findings, and at worst one pass over its results (§4 `changes`) | < 2 s at 1M results, 10k discrepant |
| Dashboard and watch evaluation | p95 < 1 s at 500 pairs × 4,400 cycles, K = 50, sign-offs 4,000 cycles back |
| Trend by day: one index range | < 300 ms over 4,400 cycles |
| `trends.csv` at 100,000 rows (a pair's points read 5,000 at a time, each page rendered at once, so no pair's measures are held whole); `dashboard.pdf`, 100 pairs | < 5 s and < 200 MB; < 10 s |
| Backfill | ≤ 10 full measures (100 tombstone-only) per 10 s: 20,000 unpurged pre-014 cycles in under 6 hours |
| A brief's two reads beyond its pair's evaluation: `brief_points` (one range of `cycles_readiness_pair_idx`, 31 rows, the 31st only for marks) and `brief_causes` (one statement over the root-cause rows of at most 30 cycles, each cycle's read by its key, a LATERAL probe with `OFFSET 0`, so the cost never grows with `ds.root_causes`) | < 1 s at 30 cycles of 20,000 groups each, in 2.6M rows |

## 15. Seams with other features

Notifications route `readiness.changed` (§8), which readiness adds as one `core/notify.EVENTS` row, its fact
model, rule keys, wording and the adversarial test. Labelled accuracy is read through `readiness_governance`
(the attested, else newest labelled, labelling of each candidate, with its `ds.labellings.accuracy`) and
`reports/accuracy.gate`, which takes that row (§9 Y18). The benchmark needs nothing. A stopped worker records
nothing, while the API still evaluates and `fresh` turns red.

Schedules ([SCHEDULES.md](SCHEDULES.md) §13) are built and read nothing of readiness; their runs are measured
as any run's (§6). The Y7 seam is open and not built: for a pair with no live soak whose spec name a
schedule runs as batch, Y7's tail could read `; schedule nightly-customers is next due
2026-10-08T01:00Z (a schedule, not a forecast): each batch run of it that ends complete adds one` in
place of `; no live soak of … will add cycles`, from a `schedules.upcoming(conn, spec_name)` that does
not exist yet. Until then such a pair reads `no live soak of … will add cycles` though a schedule is
about to add one.

Table pre-checks ([PRECHECKS.md](PRECHECKS.md) §7, §14) are built and read nothing of readiness; readiness
reads nothing of them. Their schema guard keeps one invariant this contract's streak relies on: every baseline run of one
`spec_id` read the columns its predecessor read, as the same classes, unless a person accepted the change
(a run that would not is created paused, or refused under `schemaDrift: {mode: pause}`, and a run created
paused and not accepted is no baseline). So a convergence streak never crosses an unaccepted narrowing. A
readiness gate over the guard's record is a seam, not built.

## 16. Documents to amend

CONTRACTS §1–§5, §7, §8, §10. AUTH §4.1 (five routes; transitions as projections, §12), §4.2, §4.3, §6.
RETENTION §4.1, §4.2, §8. APPROVALS §1.2 (readiness is no precondition). PACK §1.3 (counted with `packs` or
`readiness`), §2.3, §4.3 (never in a pack). SAMPLING §10 (`sample.problems(selection=)`), §14. LICENCE §1,
§2. ALERTS §2.1, §2.3, §2.4 (the reservation lifted), §3, §6.2, §6.4 (the event's labels), §10, §12 (the
routing token's canonical form), §14 (§8 here). ACCURACY §7 (`accuracy.gate` a third text builder, covered
by `test_never_stated` and the golden lines). DEPLOY §3, §10. RUNBOOK, a new "Cut-over readiness" section
(configure, census cost, check, read gates, act on the precheck, route events, close a pair, reports, after
an upgrade, when readiness fails, verify). architecture.md §4.4, §4.11, §10, §15 (K = 3, 7 days and pooled
census gates as readiness defaults, informing, never gating sign-off);
`docs/architecture/control-plane.sql`. README and PLAN.
`deploy/config/deltasleuth.example.yaml` holds the block, commented.

## 17. Tests

No image is pulled; the tests run on the local `postgres:16-alpine`.

| File | Tests |
|---|---|
| `unit/core/test_readiness.py` | `test_census_allowance_exact` (1,000 keys allow 1, 999 allow 0); `test_frame_never_census`; `test_partial_critical_breaks_streak`; `test_partial_certain_failure` (x + u_f over the pooled allowance, ab_f, api_f; never for a partial scan or a cut key set); `test_a_cycle_that_compared_no_key_fails_for_certain`; `test_explicit_list_evidence_only_with_census`; `test_streak_ends_at_version`; `test_convergence_is_not_met_when_its_unknown_points_cannot_complete_it`; `test_measures_of_another_format_are_an_unknown_point` (and a pair that cannot be evaluated); `test_signed_cycle_4000_back_in_streak`; `test_new_cycle_keeps_adjudicated_candidate`; `test_only_an_evidence_cycle_is_the_candidate` (a signed five-key explicit list, an adjudicated captured-sample replay); `test_a_candidate_of_another_run_is_named_with_its_run`; `test_sampled_tier_below_policy`; `test_accuracy_near_parity` (under `min_decided`, a census of items, one that decided nothing, `discrepant: null`); `test_earliest` (running, scheduled, a current cycle not yet due, `maxCycles` and `until` before N, paused); `test_closed_as_of` (no schedule of a past day); `test_signoff_credit_regressed_superseded`; `test_stored_signoff_survives_expiry_and_downgrade`; `test_licence_unknown_keeps_last` (a `met` never kept); `test_tokens_mode_stops_at_parity_ready`; `test_score_table` (each §5 row); `test_a_row_s_causes_are_what_differs_for_its_pair` (§8: each gate's cause, `fresh` by time or by a new cycle, the licence, settings just recorded, `regressed` alone); `test_policy_sha_deviations`; `test_failing_cycle_never_raises_state` (Hypothesis: from `not_ready` up, adding an evidence point that fails a gate other than `unknown` never raises a state); `test_messages_golden` (every gate × status, Y14's reasons, Y19); `test_brief_marks` (each mark, a key table of another size among them; none for a growing scan, consecutive samples, a point without counters or an error record); `test_brief_ratio_that_rounded_its_count_away` |
| `unit/test_config.py` | Z1–Z8; `readiness: {}` turns `counting` on |
| `unit/runner/test_readiness_measure.py` | bug-rule CRITICAL counted; `test_change_identities` (\|D_c\| = persistent + new = different + outstanding; \|D_{c−1}\| = persistent + fixed + other; a key gone from both tables counts `other`); `fails` per tier; basis mapping (scan without L2, partial scan, old counters); late, failed, back-off to `final`, excluded cycles; a statement the server cancels is a failed measure and the rest of its batch is stored, a lost connection alone ends the batch; a `start_purge` race; store and audit in one transaction; the unmeasured cycles read whole, and run by run while a backfill is owed; the series as the pure reading gives it, behind its unknown points and of another format too, and a pair without evidence counted up to the cap; `test_brief_reads_are_bounded` (a purge committed between a brief's two reads changes nothing; the reads' limits) |
| `unit/runner/test_readiness_watch.py` | baseline, then changes only; `test_upgrade_emits_only_baseline` (a backfill of pre-014 cycles writes no other row, then one baseline per pair); a wave of more than ten cycles, and a cycle ending during a pass, chain nothing of a pair until its cycles are measured; each trigger, `licence.*` included, none of them a cause; the causes a row names from its changed gates, in order; a pack issued while an evaluation reads and a sign-off revoked while no worker ran, named all the same; a chain row committed after a higher id is read within 30 s; timed `fresh`, another run's cycle beside it; closed silence; restart idempotence and `readiness.config`, named by the rows written with it and by no later one; a fall after an unrelated settings edit reaching the runbook's rule; a failed evaluation tried again 30 s later, not every pass; packs still verify; the notifier routing a row, a key-shaped pair withheld; a sampled pair's ledger read once its parity gates are met; the key census on a Community (tokens-mode) key-list soak with a table side; `since` kept while a gate changes; under SSO, a signed five-key explicit list and an adjudicated captured-sample replay never the candidate; a revoked sign-off never kept `met` by an unreadable licence; measures of another format, and a pair that cannot be evaluated, never stopping the others |
| `unit/store/test_migration_014.py` | over a store with purged, pre-006, pre-008 and pre-009 cycles and one-shots: excluded cycles marked; no rewrite; a record without `format`, or with neither `pair` nor `excluded`, refused by the CHECK; the backfill measures each (`unknown` where fields are missing) |
| `unit/api/test_readiness_api.py` | roles, `as_of`'s subset, `precheck=1` only on demand, the §9 API messages, audited downloads, volumes (and the list's count of them), the `trends.csv` refusal, no 410; the fake-IdP ladder up to `evidenced`; `test_never_told` (N1's allowed texts only, N2's schedule label, N6's suppressed count beside each discrepant one) over every answer, PDF and CSV; a no-key canary; the brief: `test_brief` (the six outcomes add up to `compared`; M8 after a run of five keys given with it; a closed pair's window), `test_brief_sampled_has_no_rate` (and unjudged), `test_brief_burn_down`, `test_brief_closed_groups_under_keys_not_compared` (M6's count of them), `test_brief_marks_a_key_table_of_another_size`, `test_brief_purged_and_empty` (both M2 forms, M3), `test_brief_reports` (the 422s; `pair` audited), and `test_never_told` over the brief and its three reports (no `%` outside N1's gate texts and `sample.lines`; no `weighted_rate`, path, key or token) |
| `unit/core/test_notify_events.py`, `test_notify_rules.py` (the notifier's) | the `readiness.changed` row: severity (a gate that only became judged, a renewed licence's row among them, is no alert), dedup key, fact model with key-shaped and unsafe planted names (`notify.skipped {reason: unsafe}` never; slugs pass), labels withheld; `programmes`, `states`, `gates`, `causes` matching (any listed cause); `causes` required. `unit/runner/test_notify_feed.py`: a fall after a delivered recovery is sent, the same fall twice in a row once; `unit/runner/test_worker.py`: a routing token computed before the rule keys existed records no `notify.config` |
| `unit/reports/test_readiness_reports.py`, `reports/test_accuracy.py` (labelled accuracy's) | the run section's variants and reproducibility (later cycles change neither an earlier cycle's section nor its place after the trend; no schedule clause; §2 over the section and `readiness.csv`); `readiness.csv` among `FORMATS`, up to the cycle, 500 rows at most, served for a purged cycle; CSV escaping of a planted formula; byte-identical packs (a pack issued after the measures, the recorded rows and a readiness download holds the facts, exports and audit rows of one issued before, and nothing of readiness); `test_brief_pdf_is_one_page` (every cap reached: the longest golden text on each of the twelve gates, eleven of them blocking, 15 strata, 30 points, 500 groups listed of 1,204, a 63-character name; the same with every line three times as long; more than a page without `compact`); `test_brief_pdf_words_a_ratio_that_rounded_its_count_away`; `accuracy.gate` golden and `test_never_stated` |
| `unit/runner/test_census.py` ([FILES.md](FILES.md) §18) | a whole scan whose side a's files held lines that were not rows measures as basis `keys`, its problem C1's text (the text Y2 prints; the gate's own line is not asserted for it); a fixed side's record and wording hold no digest |
| `unit/cli/test_cli.py` | §2 over `ds readiness` and `ds readiness pair`; `--fail-unless` exits 0 and 1; `verify` passes on a store with a one-shot's marker and the error record of a cycle never measured, and exits 2 on a named cycle whose measures were edited, replaced (an error record, a marker, a record without a pair), removed, or removed and measured again, on measures no row names, on a record that is neither and on a forged excluded marker; `report` audited; `test_readiness_report_pair` (`--pair`'s three files, each audited with `pair`; exit 2 for an unknown pair and with `--programme`, nothing written) |
| `e2e/test_soak.py` | the table demo (`examples/tables`) as a five-cycle soak under the default gates, with the seeded data fixed between cycles 2 and 3, moves from `not_ready` to `parity_ready` once three clean cycles follow; the API, the recorded transitions, `ds readiness`, `dashboard.csv`, the run's `readiness.csv` (4 keys fixed, 2 gone from both tables) and each cycle's run report agree, and none shows a key; the pair's brief shows the stored rate fall at cycle 3 and the six root-cause groups of cycles 1 and 2 `closed` |
| `web/test/readiness.test.ts` | the words and shapes of states and gates; `k of m`; the sparkline's, the step chart's and the trend's geometry, each counted cycle's suppressed keys in their titles and a sampled cycle's counts said to be its sample's; no percentage built in `ready.ts` or written in `Readiness.tsx`; a download for each `dashboard.pdf` volume; the requests; the brief's figures, a stored ratio that rounded its count away (`ratio`), where its notes stand, a group's wording and `briefChart` (strips, maxima, gaps, marks) |
| `unit/runner/test_readiness_perf.py` (`DS_PERF=1`) | the §14 targets: measuring a cycle of 1M results, 10k discrepant; over 500 pairs × 4,400 cycles with K = 50 and sign-offs 4,000 cycles back, the evaluation's p95, a trend by day, `trends.csv` at 96,800 rows (time and peak memory) and a `dashboard.pdf` volume of 100 pairs; a brief's two reads over 130 cycles of 20,000 root-cause groups (2.6M rows). The backfill's rate is the batch size the measure tests hold |

## 18. Limits, deferred, size

**Limits:** convergence is a stability rule, not a statistical test. An `unknown` point reads above `not
ready` only while it could still complete a streak. A pair's cycles are counted up to 100 where only
their number is told (Y14), the installation's unmeasured cycles up to 100,000 (Y15). A census pair's targets gate is pooled;
per-stratum targets stay the approver's unless the cycle is sampled. Thresholds are an administrator's file,
recorded but not four-eyes approved, applied at restart; stability may relax to 1 cycle or 90 days, visibly.
Computing on read recolours history. Cycles purged before 014 read `unknown`. Frequent versions restart
convergence. Accepted rules lower `different`, with `suppressed` beside it. Concurrent runs interleave. A
database owner can edit measures: `verify` detects what §12 lists, and prevents nothing; what is edited
before a cycle is measured it does not see. R-d is checked at measure time; the
precheck repeats it. Readiness may be stricter than sign-off, so a signed pair can read `regressed`. Counting
key censuses for readiness adds a snapshot read per table side per cycle. A brief's `closed` is not "fixed"
(the group's keys may not have been compared since: M6 counts those of the newest cycle only),
and a renamed path gives its groups new `RC-` ids; no `K` marks a key list replaced by one of the same size
(no key digest is stored), an inline list (changing it is a new version: `V`) or a growing scan or listing;
a rolling soak over a growing key table is lettered `K` at each cycle that read another count of keys;
sampled cycles always differ by draw (M7);
`brief.pdf` cuts gate texts and scales down past its caps.

**Deferred:** a four-eyes policy table, stopping converged soaks, forecasts, XLSX, ABAC, streamed CSVs.
The MCP server has two readiness reads, `get_readiness` and `get_readiness_pair` ([MCP.md](MCP.md) §7:
no gate text, trend, points, sign-off precheck or `as_of`); the chat's agent has no readiness tool.

**Size:** about 4,000 Python product lines (`core/readiness.py` 1,280, `deltasleuth/readiness.py`
600, the runner 290, repo 640, config 170, the API 100, reports 420, the CLI 220, the notifier's modules 160, the
rest 70), 770 of TypeScript, 23 of SQL and 6,300 test lines.
