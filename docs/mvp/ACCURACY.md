# Labelled accuracy ("which side is right"): contract

This contract is binding. It builds labelled accuracy after alerts (`012`), with migration
`013_accuracy.sql`. It amends ALERTS, APPROVALS, PACK, RETENTION, AUTH,
SAMPLING, LICENCE, CONTRACTS, BENCHMARK, RUNBOOK, DEPLOY and docs/architecture.md, as §15 lists.

**Goal:** for the discrepancies an ended cycle observed, a cut-over board learns how often the truth
favoured the legacy side, the new side, neither, or neither-matters, per root cause. Optional samples show
how often a rule, a mask or an agreement hid an error. **Precondition:** labellers must find each record in
the truth source, so the feature is usable only for `business_code` keys, or specs that show a locating
path in clear (a `security.clear` path, or `unlisted: clear`, that the policy resolves clear), until the
per-key reveal (AUTH §11) exists. A tokenised key with nothing in clear needs `acknowledge_unlocatable` at
the draw (§3), and every output then says so.

**Shape:** DeltaSleuth draws a uniform salted sample from the store. A named person labels it offline
against a truth source named at the draw. DeltaSleuth counts the labels with exact two-sided 95%
Clopper-Pearson intervals, the sign-off attests them and the pack carries them without keys. The engine
still never decides which side is correct (architecture §2.3). It reuses PACK §5's store adjudication
pattern: server salt, one upload, freeze, tombstone, audit, uploader barred from approving. No new
dependency; every claim stays *observed*.

## 1. Decisions (each one seam)

- **D1** An own uniform draw, never `adjudicate.select`, which is purposive (root causes first, outcome
  quota, MATCH share; `test_a_secondary_root_cause_gets_its_own_key_and_severe_causes_come_first`): no
  rate from it has a valid interval.
- **D2** The unit is a *discrepant item*: one discrepant key in one root cause. Accepted and agreed keys
  are one item each.
- **D3** Fixed enumerations only. Notes are accepted and discarded, as `adjudicate._reviewed` does. The
  truth source is an identifier plus an as-of date.
- **D4** Every drawn item is labelled, `UNDECIDABLE` with a reason included (`NOT_REVIEWED` for an item
  nobody reached). A partial upload is refused and changes nothing. A labelling is labelled once.
- **D5** Overall figures come from uniform items only. Intervals are exact, two-sided 95% and rounded
  outward. A whole population gets exact counts, and undecidable items get a worst-case range. No pooled,
  cross-cycle or simultaneous claim.
- **D6** Reported, never judged: only integrity refusals; circularity and dates warn; readiness may define gates.
- **D7** A sign-off attests the cycle's newest labelling (by `labelling_id`, the drawn order, as PACK §5)
  that a person (`user:`) labelled. The labeller joins the exclusion set. Independence from the run is
  attested, not enforced.
- **D8** Every auth mode, tier and evaluation, and an open build, which has no licence (LICENCE O2, §19, SAMPLING D10). Token and developer-mode
  labellings show as not attested.
- **D9** No sign-off-stratum breakdown (SAMPLING D5, D12, §11.5).
- **D10** No outbound call and no truth connector (black-box principle).
- **D11** Packs stay `deltasleuth-pack/1` with VERIFY.txt unchanged and carry a labels file without keys,
  item numbers or the sheet's order (§9 states what a reader who holds masked keys can still confirm).
  `ds packs verify` adds a line after step 5, as SAMPLING §11.4 did.
- **D12** Attests format 3 (alerts take no format).
- **D13** No AI label or AI-computed figure. The agent only explains stored numbers.
- **D14** Frozen while the cycle has a requested or approved sign-off.
- **D15** A repeated draw (same actor, cycle, parameters and truth, unlabelled, within 10 minutes) returns
  the first labelling instead of drawing again.

## 2. What is labelled (`reports/accuracy.py`)

### 2.1 Populations

| Population | Results (`types.derive_verdict`) | Item |
|---|---|---|
| `discrepant` | DIFFERENT: MISMATCH, NOT_FOUND_A/B, KNOWN_DIFF under a `bug` rule | each distinct `root_cause_id` of the key's findings; `*` when none has one (a NOT_FOUND whose document finding a mask suppressed; a bug rule on an INFO finding) |
| `accepted` | MATCH through KNOWN_DIFF (non-bug rules) or NOISE_ONLY | `*` per key; domains: each suppressing `rule_id`, `mask` for none |
| `agreed` | outcome MATCH | `*` per key |

Never labelled, each counted (`indeterminate`, `not_compared`, `matched_by_row_hash`: in
`selection.counts` and the score, which the API's item, `accuracy/score.json` and
`accuracy/attempts.json` carry; `lines` prints none of them, LIMITS says what they leave uncovered):
INCONCLUSIVE and DRIFT keys; the cycle's keys with no result; rows matched by row hash
(`ds.scans.state->>'matched'`: no per-key result, so a scan's agreed keys are rows equal once normalised).

### 2.2 Labels

Sides are `a` and `b`, and every text adds the spec's `role`. Each item is labelled as the truth stood at
its **`observed_at`** (the later of its two observations', else the cycle's `ended_at`), which the sheet
shows.

| Label | MISMATCH, bug KNOWN_DIFF | NOT_FOUND_B (only a holds it) | NOT_FOUND_A (only b holds it) | Accepted or agreed key |
|---|---|---|---|---|
| `A_RIGHT` | truth holds a's values, not b's | the record should exist, as a holds it | it should not exist | a hidden difference, a right |
| `B_RIGHT` | b's, not a's | it should not exist (a is stale) | it should exist, as b holds it | likewise b |
| `BOTH_WRONG` | neither side's | it should exist, with values other than a's | it should exist, with values other than b's | a shared error |
| `BOTH_RIGHT` | the difference does not matter | the absence does not matter | the absence does not matter | rightly equal or accepted |
| `UNDECIDABLE` | the truth at `observed_at` cannot be established | same | same | same |

- **Several findings in one item** (members of one cause): leave out those whose difference does not
  matter; `A_RIGHT` if the truth favours a on all the rest, `B_RIGHT` if b, `BOTH_WRONG` if neither or
  split (a on some, b on others), `BOTH_RIGHT` if none is left; `UNDECIDABLE` if the truth cannot be
  established for one that matters.
- **`basis`:** required for a decided label and empty otherwise: `SYSTEM_OF_RECORD` (an authoritative
  system other than both compared sides), `SOURCE_DOCUMENT`, `BUSINESS_RULE`, `OWNER_CONFIRMED`.
- **`reason`:** required for `UNDECIDABLE` and empty otherwise: `NOT_LOCATABLE` (not found in the truth
  source), `TOKENISED` (not findable because the key or locator is tokenised), `NO_TRUTH`,
  `TRUTH_CONFLICTS`, `CHANGED_SINCE` (the source no longer shows the state at `observed_at`),
  `NOT_REVIEWED` (nobody reached it; counted apart, §7, §9).

### 2.3 The truth source

`truth: {source, as_of, names_side}` is fixed at the draw and printed with every share.
- `source` is a system identifier (`^[A-Za-z][A-Za-z0-9_.:/-]{0,63}$`, e.g. `CBS-PROD.account_master`),
  never free text, checked by `rawvalues.key_shaped` against the run's spec.
- `as_of` is the date of the truth source's state the labellers use, no later than the draw's (UTC).
  Outside the drawn items' `observed_at` dates it warns (§3).
- **Circular truth.** `names_side` (`"a"`, `"b"` or null) is set when `source`, ignoring case, equals a
  side's target id, a table side's `name` or `schema.name`, or starts with the target id followed by `.`,
  `:` or `/`. It warns at the draw and in every output (§7, §9). Labels resting on a compared side are
  circular and measure nothing; a source the check misses is the labeller's responsibility (LIMITS).

## 3. The draw (`accuracy.store_draw`)

`store_draw(ctx, run_id, cycle, *, who, size=100, per_cause=2, accepted=0, agreed=0, truth,
acknowledge_unlocatable=False)`:
- `size` 0–1,000 uniform discrepant items; `per_cause` 0–20; `accepted` and `agreed` 0–500 keys each;
- at least one above 0, and `size + accepted + agreed` ≤ `MAX_ITEMS = 1000`.

Steps:
1. Parameters (422: the sizes, then the truth's source and date), the run (404), a key-shaped source
   (422, judged against the run's spec), then `adjudicate._unfrozen`, which gains `what`
   (`"adjudication"` keeps today's message), here `"labelling"`.
2. **Repeat:** an unlabelled labelling of the cycle drawn by `who` in the last `REPEAT_S = 600` s with the
   same `size, per_cause, accepted, agreed, truth` and acknowledgement is returned (200, §11's repeat
   warning); nothing is drawn. A retry after a proxy timeout or a client disconnect (the server finishes
   the draw) therefore yields the first labelling. A retry without the acknowledgement a tokenised key
   needs is another draw, and step 3 refuses it.
3. **Unlocatable:** a key not `business_code` with nothing shown in clear needs
   `acknowledge_unlocatable`, else 422; the acknowledgement is stored (`selection.unlocatable`: true for
   such a key, which was drawn only acknowledged). Nothing is shown in clear when no path resolves clear
   under the compiled policy (`security.policy.canonical_policy`): none of `security.clear`'s entries
   does (a tokenise rule of a PII class voids a clear entry beneath it), nor the root `$` where
   `unlisted` is `clear`.
4. The salt is `sampling.nonce()` (128 bits of hex holding a letter: the pack's gate reads it as
   generated).
5. **`repo.labelling_draw`**, one statement in a transaction of its own, under `SET LOCAL
   statement_timeout` of `DRAW_TIMEOUT_S = 300` s (`QueryCanceled` becomes `accuracy.DrawTimeout`, 504,
   nothing stored) and `SET LOCAL enable_nestloop = off`. The cycle's results and discrepant findings are
   each read once, and every item is hashed once:

   ```sql
   WITH res AS MATERIALIZED (       -- population from outcome and the _BUG fragment of repo.stratum_counts
       SELECT key_masked, <population> AS population FROM ds.results WHERE run_id = %(r)s AND cycle = %(c)s),
   caused AS MATERIALIZED (
       SELECT DISTINCT f.key_masked, f.root_cause_id AS cause
       FROM ds.findings f JOIN res USING (key_masked)
       WHERE f.run_id = %(r)s AND f.cycle = %(c)s AND f.root_cause_id IS NOT NULL
         AND res.population = 'discrepant'),
   items AS MATERIALIZED (
       SELECT i.*, sha256(convert_to(%(salt)s || chr(31) || i.key_masked || chr(31) || i.cause, 'UTF8'))
                   AS digest
       FROM (SELECT 'discrepant' AS population, key_masked, cause FROM caused
             UNION ALL
             SELECT population, key_masked, '*' FROM res
             WHERE population = 'discrepant'
               AND NOT EXISTS (SELECT 1 FROM caused WHERE caused.key_masked = res.key_masked)
             UNION ALL
             SELECT population, key_masked, '*' FROM res
             WHERE (population = 'accepted' AND %(accepted)s > 0)
                OR (population = 'agreed' AND %(agreed)s > 0)) AS i)
   ```

   **Its cost does not depend on the planner's estimates.** With `enable_nestloop` off the joins of the
   cycle's rows (`caused`, the keys without a cause, the rules) are hash or merge joins, one pass over
   each side, whatever rows the planner expects. A new store's first cycle end analyses its tables before
   any finding has a root cause (`repo.analyse_new`), so the statistics say none has one and the planner
   takes the cycle's linked findings for one row: a nested loop over them would meet every result once per
   linked finding, quadratic in the cycle's size. For the same reason
   `repo.replace_root_causes` runs `ANALYZE ds.findings (root_cause_id)` in its transaction when the
   statistics hold under half the findings it linked, so other readers of the column are estimated right
   too. The `_BUG` fragment names the run and cycle (`f.run_id = %(r)s AND f.cycle = %(c)s AND
   f.key_masked = k.key_masked`), in `repo.stratum_counts` as well: correlated on the result's own
   columns, its hashed subplan read every run's findings.

   It returns, by a `kind` column:
   - **`uniform`** (discrepant, accepted, agreed): per population `ORDER BY digest, key_masked COLLATE "C",
     cause COLLATE "C" LIMIT k` (a top-N heapsort holding k rows). N ≤ `size` is a census.
   - **`coverage`:** `row_number() OVER (PARTITION BY cause ORDER BY digest, key_masked COLLATE "C") <=
     per_cause` over discrepant items with a cause (a full sort, only when `per_cause` > 0).
   - **Counts:** `cause` rows, a cause's items from `items` with its `ds.root_causes.seq` (`*` among them;
     the planner may sort once more to group them); `keys` per population, the `indeterminate` ones too,
     from `res`; `not_compared` (the cycle's `run_keys` count less its results: every result has its key);
     `matched_by_row_hash`; `rule` rows, accepted keys per rule from their suppressed findings, `mask`
     for a key no rule suppressed (a second findings read, only when `accepted` > 0).

   `bytea` order equals Python's byte order and `"C"` equals code-point order, so the SQL equals
   `sampling.digest(salt, f"{key_masked}\x1f{cause}")` sorted. `load_run` is never called.
6. Python (`accuracy.select`) tops causes up in `seq` order to their `min(per_cause, N_h)` smallest items,
   item by item, and stops at `MAX_ITEMS` (the last cause reached may hold fewer; the causes left without
   any item are counted and warned about), reads the drawn items' sheet columns and `observed_at`
   (`repo.labelling_items`, at most 1,000, in a second transaction, which then re-checks `_unfrozen`: the
   draw holds no lock on the cycle, and a purge that deleted its results since the draw took its rows is
   answered 410), sets `names_side` and builds the sheet. A blank sheet over half of
   `adjudicate.MAX_UPLOAD` is refused, so 512 KiB stay free for notes.
7. One `repo.audited` transaction re-checks `_unfrozen` and the repeat (a racing retry gets the other's
   labelling), runs `repo.insert_labelling` and audits `accuracy.draw`.

**Validity.** Take the salt as random and SHA-256 as a random function. The uniform part is a simple
random sample without replacement (SRSWOR) of all discrepant items. A cause's uniform items are its
smallest below the uniform cut, and coverage tops it up with its next smallest, so a cause holds its m_h
smallest digests; given m_h they are an SRSWOR of the cause. So overall figures use uniform items only,
and a cause's figures use all its items.

**Sheet** (`V-….csv`, `render_csv`). Columns are `item key population root_cause_id path_pattern
change_type rules engine_outcome selected_for observed_at obs_a_id obs_b_id label basis reason notes`.
Rows run by population (discrepant, accepted, agreed), then in digest order; `selected_for` is `uniform`
or `coverage`; `rules` holds an accepted key's suppressing rule ids, space-separated (`mask` for none; empty
for the other populations). `selection_sha256` hashes the lines
`item⇥key_masked⇥population⇥root_cause_id⇥selected_for\n`. `selection` is `{items (the list of `{item, key,
population, root_cause_id, selected_for}`), counts, causes, uncovered, names_side, unlocatable, warnings}`:
`counts` is `{discrepant: {items, keys, causes, drawn}, accepted: {keys, by_rule, drawn}, agreed: {keys,
drawn}, indeterminate, not_compared, matched_by_row_hash}`; `causes` the drawn causes in rank order
(`[{root_cause_id, rank, items}]`, rank `seq + 1`); `uncovered` how many causes have no item.

**Warnings** (in the 201 answer and `selection.warnings`):
- `truth.source LEGACY-DB.accounts names side a (legacy): labels resting on a compared side are circular
  and measure nothing; name an authoritative system other than both sides (docs/mvp/ACCURACY.md §2.3)`;
- `the key is tokenised (class account_number) and the spec shows no path in clear: expect UNDECIDABLE
  (TOKENISED) rows (acknowledged)`;
- `truth.as_of 2026-09-20 is outside the drawn items' observations (2026-10-01 to 2026-10-02): label each
  item as the truth stood at its observed_at, UNDECIDABLE (CHANGED_SINCE) where the source cannot show it`;
- `per_cause 2 covered 180 of 300 root causes within the 1,000 items a labelling holds; 120 have no item
  of their own`;
- `4,998,916 rows matched by row hash cannot be sampled: agreed keys are the 4,210 read per key`.

**Relations.** The adjudication has its own salt and sheet, and an item may be on both. It checks the
engine, a labelling checks the sides, and PACK D1 stays on it. A hash-sampled cycle's populations are its compared
keys, members and contrast alike. A labelling never reads `run_keys` labels or the nonce, and never weights to the
frame.

## 4. Who labels, and finding records

- **Roles.** `operator` draws and uploads; `viewer` lists and downloads. The uploader is the labeller of
  record and answers for SMEs who filled the sheet offline, whom DeltaSleuth does not record. Only a
  `user:` labelling is attested. The approver checks which labelling is used, its counts, truth, labeller
  and independence, never a single label (LIMITS says so).
- **Independence**, attested and printed, never refused: `started_run` (the labeller is in
  `ds.accountable(run.created_by)`) and `spec_author` (in the cited spec approval's `authors`). A
  scheduled run is created by `system:worker`, which answers for nobody, so its labellings read
  `started_run` false whoever labels them, the schedule's setter included
  ([SCHEDULES.md](SCHEDULES.md) §6).
- **Finding records.** The sheet holds masked keys. Labellers open items on the Key screen (`GET
  /v1/runs/{id}/keys/{key}`, audited `key.view`) and locate records through a `business_code` key or a
  path approved `clear`. Otherwise the row is `UNDECIDABLE` (`TOKENISED` or `NOT_LOCATABLE`), which widens
  the worst case; only a business rule can decide it without the record. The per-key reveal (AUTH §11)
  is a later dependency, not built here.

## 5. Upload and scoring (`accuracy.store_labels`)

As `adjudicate.store_score`:
1. `text/csv` of at most `adjudicate.MAX_UPLOAD` (1 MiB); found, `_unfrozen`, unlabelled (the order of
   `store_score`: a purged cycle answers 410 whether or not its labelling was labelled, §10).
2. `adjudicate._read_sheet` gains keyword arguments `required` (here `item key population root_cause_id
   engine_outcome label basis reason`) and `again` (defaults keep today's messages), and refuses a header
   that names a column twice (a row would keep the last one's cell, not the one the uploader reviewed;
   the adjudication's reviewed sheet too). `csv.parse_csv` reads a cell as long as the upload (the CSV
   reader's own limit is 131,072 characters) and turns what the reader refuses (`csv.Error`) into the
   ValueError `_read_sheet` answers 422 for. `accuracy._check_rows` runs §11's checks in order (the engine
   columns are `key population root_cause_id engine_outcome`: a spreadsheet may reformat the others); the
   pure `accuracy.score_rows(rows, sheet, selection, truth)` gives the kept sheet and its score.
3. The drawn sheet is kept with label, basis and reason upper-cased and notes emptied
   (`accuracy._labelled`). Only those three columns come from the upload, and the score is tallied from
   the kept sheet: a `selected_for` or `rules` cell edited in a spreadsheet changes nothing.
4. One `repo.audited` transaction: `_unfrozen`, `repo.label_labelling(… WHERE labelled_at IS NULL
   RETURNING *)` (None gives 409, `labelled_at = now()`), `accuracy.label`.

Hashes: `sheet_sha256`; `labels_sha256` over `accuracy.keyless(sheet)` (columns `population root_cause_id
path_pattern change_type rules engine_outcome selected_for label basis reason`, the rows sorted by these
cells: no item number, and not the sheet's order, which follows the keys' digests); `score_sha256` =
`sha256_hex(jsonio.dumps(accuracy, sort_keys=True))`. No hash of the upload is taken: the kept sheet holds
everything of it but the notes, so it would be a hash of the notes (§12). The score holds no actor or time
(`labelled_by` and `labelled_at` are the row's, bound by attests and the chain), so it is computed before
the transaction. It is stored once; `accuracy.tally(keyless, selection, truth)` re-derives it (the
selection's `counts`, `causes`, `uncovered` and `unlocatable`, which a purge keeps; rules in rule-id
order, whatever order the stored selection's keys come back in).

## 6. Statistics

### 6.1 `core/sampling.py`

- `upper(x, n, alpha=_ALPHA)`: `math.log(alpha)` replaces `_LOG_ALPHA`. The default leaves `passes`,
  `needed`, `x_max` and §8.3's table unchanged.
- `interval(x, n) -> (str, str)`: the lower end is `1 - upper(n - x, n, 1/40)`, `"0"` at x = 0; the upper
  end is `upper(x, n, 1/40)`, `"1"` at x = n; both round outward to 4 significant digits (a new `_FLOOR`,
  ROUND_FLOOR; `_SHOWN`), so a shown interval contains the exact one.

### 6.2 The score (`accuracy`)

`accuracy.share(x, n, N)` gives `{x, n, share, low, high, items_low, items_high}`: `share` is x/n to 4
significant digits, half-even (`"0"` at x = 0, null at n = 0); `items_low = ⌊N·low⌋` and `items_high =
⌈N·high⌉`, for label shares only (null elsewhere); a census has null bounds and exact counts. Where u of
the items are undecidable (those nobody reviewed among them), every share adds `worst_case`, the range
counting them either way (as none of the x, or all), null when u = 0:
- a share of d decided items: `{low: interval(x, d + u).low, high: interval(x + u, d + u).high}` (a
  census: x/(d+u) and (x+u)/(d+u), rounded outward);
- a label's share of the n items (not `UNDECIDABLE`'s own): `{low: interval(x, n).low, high: interval(x +
  u, n).high}` (a census: x/n and (x+u)/n), and its `items_low` and `items_high` then come from that
  range (`⌊N·low⌋`, `⌈N·high⌉`; a census: x and x + u), since an undecidable item may be any label's.

Every share is text, so jsonb keeps the score as hashed.

- **Top level:** `method: "deltasleuth-labelled/1"`, `confidence: "0.95"`, `sided: "two-sided"`, `truth`,
  `unlocatable`, `indeterminate`, `not_compared`, `matched_by_row_hash`.
- **`discrepant`:** `{items, keys, causes, uniform, census}`; `labels` (shares over the n uniform items);
  `reasons` and `basis_codes` counts; `sides.{a,b}` with `right_or_immaterial` = (own `_RIGHT` +
  `BOTH_RIGHT`)/d and `wrong` = (other `_RIGHT` + `BOTH_WRONG`)/d; `b_share_of_single_side_right` =
  `B_RIGHT`/(`A_RIGHT` + `B_RIGHT`), d being that sum; `by_outcome` (label counts per engine outcome);
  `by_cause` (`[{root_cause_id, rank, items, labelled, census, labels, sides}]`, all of a cause's items,
  `labels` as counts); `uncovered`. Null when no discrepant item was drawn.
- **`accepted`:** null, or `{keys, drawn, census, labels, hidden_error, by_rule: [{rule_id, keys, labelled,
  labels, hidden_error}]}`: `by_rule` lists every domain of the accepted keys, `mask` among them, with
  `labels` as counts.
- **`agreed`:** null, or `{keys, drawn, census, labels, hidden_error}`, where `hidden_error` = (`A_RIGHT`
  + `B_RIGHT` + `BOTH_WRONG`)/d.

### 6.3 Worked example (test values; oracle scipy `beta.ppf` to 1e-12)

Side a is `legacy`, b `new`. The cycle has 1,204 discrepant items over 1,100 keys and 23 causes. The 120
uniform items are `A_RIGHT` 12, `B_RIGHT` 84, `BOTH_WRONG` 3, `BOTH_RIGHT` 18 and `UNDECIDABLE` 3
(`NOT_LOCATABLE` 2, `NOT_REVIEWED` 1); the 117 decided rest on `SYSTEM_OF_RECORD` 110 and `BUSINESS_RULE` 7.

| Figure | x/n | Share | Exact | Shown |
|---|---|---|---|---|
| `B_RIGHT` | 84/120 | 70.00% | [0.609603890578, 0.780243500759] | 60.96% to 78.03%; worst 60.96% to 80.26% (733 to 967 of 1,204) |
| `A_RIGHT` | 12/120 | 10.00% | [0.052747786390, 0.168165212703] | 5.274% to 16.82%; worst 5.274% to 19.78% (63 to 239) |
| `BOTH_WRONG` | 3/120 | 2.500% | [0.005185574958, 0.071323194157] | 0.5185% to 7.133%; worst 0.5185% to 10.57% (6 to 128) |
| `BOTH_RIGHT` | 18/120 | 15.00% | [0.091389572528, 0.226667143413] | 9.138% to 22.67%; worst 9.138% to 25.50% (110 to 308) |
| b wrong | 15/117 | 12.82% | [0.073565244544, 0.202625999302] | 7.356% to 20.27%; worst 7.167% to 22.67% |
| a wrong | 87/117 | 74.36% | [0.654616843019, 0.819837777393] | 65.46% to 81.99%; worst 63.60% to 82.46% |
| b right or immaterial | 102/117 | 87.18% | [0.797374000698, 0.926434755456] | 79.73% to 92.65%; worst 77.33% to 92.84% |
| a right or immaterial | 30/117 | 25.64% | [0.180162222607, 0.345383156981] | 18.01% to 34.54%; worst 17.54% to 36.40% |
| b share of single-side right | 84/96 | 87.50% | [0.791828111563, 0.933710904257] | 79.18% to 93.38%; worst 76.24% to 93.58% |
| cause RC-3f9a0c1d2e4b, b wrong | 3/22 | 13.64% | [0.029055851129, 0.349122097257] | 2.905% to 34.92% |

- **The labels' worst case** counts the 3 undecidable items as that label's: its upper end is the exact
  upper end of (x + 3)/120, which is 0.802544872174 for `B_RIGHT` (87), 0.197776410352 for `A_RIGHT` (15),
  0.105653854519 for `BOTH_WRONG` (6) and 0.254980027566 for `BOTH_RIGHT` (21); the item counts are
  `⌊1,204·low⌋` and `⌈1,204·high⌉` of the shown range. With no undecidable item a label has no worst
  case and its counts are its interval's (84/117: 62.72% to 79.73%, 755 to 960).
- **Agreed.** 100 of 4,210 keys: `BOTH_RIGHT` 97, `BOTH_WRONG` 1, `B_RIGHT` 1, `UNDECIDABLE` 1. `BOTH_WRONG`
  is 1/100 = 1.000%, shown 0.02531% to 5.446%, worst 0.02531% to 7.039% (exact upper end of 2/100:
  0.070383932471; 1 to 297 keys); `hidden_error` is 2/99 = 2.020%, shown 0.2455% to 7.108%, worst 0.2431%
  to 8.518%.
- **Accepted.** 50 of 180 keys: `BOTH_RIGHT` 45, `A_RIGHT` 3, `B_RIGHT` 1, `UNDECIDABLE` 1 (`NO_TRUTH`).
  `hidden_error` is 4/49 = 8.163%, shown 2.269% to 19.61%, worst 2.222% to 21.82%. Rule `fx_rounding` (110
  keys) is 3/31 = 9.677%, shown 2.041% to 25.76%.
- **Pinned:** (0,10) 0% to 30.85%; (5,5) 47.81% to 100%; (1,20) 0.1265% to 24.88%.

**Sizing (RUNBOOK).** At p = ½ the interval is 35.52–64.48% at n 50, 39.83–60.17% at 100, 42.86–57.14% at
200 and 44.99–55.01% at 400. At x = 0 the upper end (a 97.5% one-sided bound) is 7.113%, 3.622%, 1.828%
and 0.9180%. The draw dialog hints: `100 items give an interval about ±10 points wide at worst; 400 about
±5`.

**Without replacement.** The binomial interval is checked against the hypergeometric, never proven for
every population. Exhaustively, the minimum coverage is 97.13% for N ≤ 40 (N 39, n 7, D 16) and 96.25% for
N ≤ 80 (N 80, n 7, D 33 or 47). Over every D at N 200, 1,000 and 5,000 with n 20, 50, 100, 120 and 400 it
is at least 95.19% (N 5,000, n 50). As N grows the hypergeometric tends to the binomial, where
Clopper-Pearson covers at least 95%. LIMITS says "in every population checked".

## 7. What it claims

`accuracy.LIMITS` is printed verbatim in report.pdf, the P0 summary, the certificate and the UI (from the
API):

"Observed. Labels record what the named labeller found in the truth source named when the sample was
drawn and in the sources their basis codes name, as the truth stood when each item was observed;
DeltaSleuth cannot see or check them, and a label never changes an outcome, a root cause or a rule. The
uploader answers for labels made offline by people DeltaSleuth does not record; a sign-off attests which
labelling was used and its counts, never that any single label is right. Labels resting on a compared side
are circular and measure nothing. Where the key is tokenised and no path is shown in clear, labellers
cannot find records in the truth source until a per-key reveal exists. The installation drew the sample
after the cycle ended, with a salt it chose and committed with the sample to the audit trail. A cycle may
be drawn and labelled more than once, and an interval holds only for a sheet that was not chosen after its
labels were seen. The discrepancy shares describe the discrepancies this cycle observed (one key's
difference in one root cause): which side the truth favoured on those, not whether either system is
correct overall. Each interval is an exact two-sided 95% Clopper-Pearson interval computed as for a sample
drawn with replacement; for this sample, drawn without replacement, it covered the true share 95% of the
time or more in every population checked, which is not a proof for every population. Each is a separate
statement, not all guaranteed at once, and covers sampling error only, not labelling error. A label's
share is of every sampled item and counts the undecidable ones, including those nobody reviewed, as not
that label's; shares among decided items assume they are like the rest; the range beside each counts them
either way and, for a label, gives its item counts. A root cause's or a rule's figures come from its own
items and describe it only.
Agreed and accepted samples bound how often an agreement, a rule or a mask hid an error among the keys read
per key. Nothing here covers keys the cycle did not compare, left unresolved or matched by row hash; keys
outside the compared population, filters, columns and time window; a sampled cycle's frame beyond its
sampled keys; other cycles; or the correctness of the truth sources."

**`accuracy.lines(row, *, signoff)`** (`signoff`: the attesting sign-off's id, or None) is printed whole,
never a line alone. Golden (§6.3, attested):

```
Labelling V-… drawn 2026-10-02 by amy, labelled 2026-10-04 by bob against CBS-PROD.account_master as of 2026-10-01, attested by sign-off A-….
Discrepancies: 120 of the 1,204 that cycle 2 of run R-… observed (one key's difference in one root cause; 1,100 keys, 23 root causes), drawn uniformly; side a is legacy, side b is new; each label is the truth when its item was observed.
Undecidable: 3 of 120 (2 NOT_LOCATABLE, 1 NOT_REVIEWED); the ranges below count them either way.
Truth favoured b (new) alone: 84 of 120 (70.00%, 95% interval 60.96% to 78.03%; 60.96% to 80.26% counting the undecidable either way: 733 to 967 of the 1,204).
Truth favoured a (legacy) alone: 12 of 120 (10.00%, 95% interval 5.274% to 16.82%; 5.274% to 19.78% counting the undecidable either way: 63 to 239 of the 1,204).
Truth held neither side's values: 3 of 120 (2.500%, 95% interval 0.5185% to 7.133%; 0.5185% to 10.57% counting the undecidable either way: 6 to 128 of the 1,204).
The difference did not matter: 18 of 120 (15.00%, 95% interval 9.138% to 22.67%; 9.138% to 25.50% counting the undecidable either way: 110 to 308 of the 1,204).
b (new) wrong (truth favoured a, or neither): 15 of 117 decided (12.82%, 95% interval 7.356% to 20.27%; 7.167% to 22.67% counting the undecidable either way).
a (legacy) wrong (truth favoured b, or neither): 87 of 117 decided (74.36%, 95% interval 65.46% to 81.99%; 63.60% to 82.46% counting the undecidable either way).
Where one side alone was right, it was b (new) on 84 of 96 (87.50%, 95% interval 79.18% to 93.38%; 76.24% to 93.58% counting the undecidable either way).
Basis of the 117 decided: 110 SYSTEM_OF_RECORD, 7 BUSINESS_RULE.
Root cause RC-3f9a0c1d2e4b (rank 1 of 23, 212 items): of 22 labelled, b (new) alone 17, a (legacy) alone 2, neither 1, did not matter 2; b (new) wrong on 3 of 22 decided (13.64%, 95% interval 2.905% to 34.92%).
Agreed keys (outcome MATCH): 100 of 4,210 drawn; did not matter 97, b (new) alone 1, neither 1, undecidable 1; an agreement hid an error on 2 of 99 decided (2.020%, 95% interval 0.2455% to 7.108%; 0.2431% to 8.518% counting the undecidable either way).
Accepted keys (a rule or mask hid the difference): 50 of 180 drawn; did not matter 45, a (legacy) alone 3, b (new) alone 1, undecidable 1; a rule or mask hid an error on 4 of 49 decided (8.163%, 95% interval 2.269% to 19.61%; 2.222% to 21.82% counting the undecidable either way).
Rule fx_rounding (110 accepted keys): hid an error on 3 of 31 decided (9.677%, 95% interval 2.041% to 25.76%).
```

- The first line is one of §11's report texts. Right after it come, when they apply: `Labelling V-… is one
  of 3 drawn for cycle 2 (2 labelled).` (the cycle has more than one labelling: every output that prints
  the lines says so, the reports and the Run screen as the certificate does; `repo.get_labelling` counts
  them as `cycle_drawn` and `cycle_labelled`); `Warning: the truth source names side a (legacy): labels
  resting on a compared side are circular and measure nothing.`; `Warning: the key is tokenised and the
  spec showed no path in clear: labellers could not find records in the truth source.`; `Warning: the
  labeller started run R-…: the labels are not independent of the run.` (or `wrote spec approval A-…`).
- The `Undecidable:` line comes before the label lines, whose ranges it announces; its reasons, and the
  basis codes, are listed in their enumerations' order (§2.2), whatever order the stored score's keys
  come back in. With no undecidable item it reads `Undecidable: none.`, no line has a "counting" clause
  and a label's item counts are its interval's (`95% interval 62.72% to 79.73%: 755 to 960 of the 1,204`).
- A census replaces `95% interval … ` with `every item labelled: exact` and states no item counts; with
  undecidable items its label lines read `3 of 6 (50.00%, every item labelled: exact; 50% to 83.34%
  counting the undecidable either way: 3 to 5 of the 6)`.
- Cause lines run in rank order, at most 20, then `3 more root causes: GET /v1/labellings/V-… lists them
  (accuracy/score.json in a pack attesting it)`: the API's item holds every cause for every reader, in
  every auth mode; the file exists only in a pack whose sign-off attests the labelling. Absent populations
  drop their lines.
- Counts leave out a label nobody gave. Rule lines name rules with a labelled key in rule-id order, at
  most 20 (then `3 more rules: GET /v1/labellings/V-… lists them (…)`), never the `mask` domain (the
  score holds it). With no uniform item (`size` 0) only the `Discrepancies:` line and the cause lines are
  printed; with no decided item a share reads `0 of 0 decided (no item decided)`; with no single-side
  item its line is dropped. `lines` of a labelling not labelled yet is empty.

**`accuracy.summary(row, *, signoff, named=True)`** is the one-line form (CLI, run lists):
`V-… run R-… cycle 2: 120 of 1,204 discrepancies labelled 2026-10-04 by bob against
CBS-PROD.account_master as of 2026-10-01 (attested by A-…): b (new) alone right 84, a (legacy) alone right
12, neither 3, did not matter 18, undecidable 3; b (new) wrong on 15 of 117 decided (7.356% to 20.27%;
7.167% to 22.67% counting the undecidable either way)`. It appends `; CIRCULAR: the truth source names side
a` and `; records not locatable (tokenised key)` when they apply; unattested it reads `(no sign-off attests
it)`; unlabelled, `…: drawn 2026-10-02 by amy against CBS-PROD.account_master as of 2026-10-01; not
labelled`; with no discrepancy drawn, the agreed or accepted hidden-error clause replaces the counts
(`an agreement hid an error on 2 of 99 decided (…)`; with neither, `no uniform item: its root causes'
figures only`); a census reads `(every item labelled: exact)`. With `named=False` (the agent's, §10) a
labelled labelling's line names no actor: `labelled 2026-10-04 by a person`, `through a token`, `in
developer mode`, or `by no signed-in person` for any other actor.

**`accuracy.gate(row, *, at, min_decided, max_b_wrong, signoff=None)`** is the third text builder, cut-over
readiness's ([READINESS.md](READINESS.md) §5, §9 Y18): the status of a pair's `accuracy` gate (`met` or
`not_met`) and its lines over the candidate cycle's labelling as `repo.readiness_governance` reads it. It
decides nothing about a labelling or a sign-off, and prints no figure of its own: a verdict line (`labelling
V-… of cycle 14 meets (or: does not meet) the accuracy gate, which allows 0 decided items where side b was
wrong and needs 50 decided (all, for a census of the discrepant items)`, or `… decided 40 items, under the
50 the gate needs` (a census is judged on its decided items however few, but `decided 0 items, under the 1
the gate needs` when it decided none), or `no labelling of cycle 14 of run R-… labels its discrepant items:
draw and label one (Labelled accuracy, Run screen)`), then `lines`' first line without its people (who drew
it is left out, and who labelled it is the kind of actor alone, as with `named=False`: readiness names no
person) and its side-b wrong share verbatim, through the helpers `lines` itself uses. `test_gate_golden`
pins its lines and `test_never_stated` covers them.

**Never stated** (`lines`, `summary`, `LIMITS` and `gate` are the only text builders; the API returns their text,
so the UI builds none; tests hold it): that DeltaSleuth decided which side is correct; either system's
overall accuracy; a share without population, n, interval, truth, labeller and date; a pooled figure; a
key's stratum; values or notes; "certified"; an AI label; that undecidable items may be ignored;
`BOTH_RIGHT` counted as a side being right.

## 8. Sign-off (APPROVALS amendments)

`approvals._format_3` is format 2 plus `labellings: {drawn, labelled}`, the cycle's labellings counted
whether or not one is attested, and `accuracy`: null unless a person labelled a labelling of the cycle,
else the newest such by `labelling_id` (`repo.attested_labelling`, which reads the cycle's row FOR KEY
SHARE first, as `attested_adjudication` does, so a draw or an upload in flight commits before it):

```
{labelling_id, salt, truth: {source, as_of, names_side}, unlocatable, selection_sha256, labels_sha256,
 score_sha256, drawn_by, labelled_by, labelled_at,
 labeller: {started_run, spec_author},
 discrepant: {items, uniform, census, labels: {A_RIGHT: x, …}, reasons: {NOT_REVIEWED: x, …}} | null,
 accepted: {keys, drawn, labels} | null, agreed: {keys, drawn, labels} | null}
```

Counts are exact; `discrepant.labels` and `reasons` count uniform items only (coverage items are in the
score); `score_sha256` binds the intervals; the freeze keeps `labellings` stable, so an approver attests
that a labelling no person labelled exists even when none is attested. `attests` records format 3;
`rebuilt` gains `case 3` (`repo.cycle_labelling`), formats 0–2 unchanged; `signoff_problems` is unchanged.
`labeller.started_run` and `spec_author` are booleans, read from the labelling's row, which
`repo.get_labelling` gives with its actors' names, its sides' roles and its independence (`started_run`;
`spec_author`, the cited spec approval's id or null).
`GET /v1/approvals/{id}` shows `labellings` and `accuracy` in `attests` and adds to `review` its `lines`
(attested while the request is open or approved) and `labellings` (`{labelling_id, drawn_by, drawn_at,
item_count, labelled_by, labelled_at, attested}`). `_why` gains `you labelled its labelling V-…` after the
scorer's text, before the closer's.

## 9. Evidence packs (PACK amendments)

`pack._judged` accepts formats `(1, 2, 3)`; `_facts` loads the cycle's labellings and re-hashes the
attested one (§11: its kept sheet's labels and its score, which its `accuracy.label` row in the segment
must hold too); the segment's objects gain their `V-` ids. A format 1 or 2 sign-off's pack is as before:
none of the files, the manifest member, the section or the verify line.

**Files:**
- `accuracy/labels.csv` (`keyless(sheet)`: no key, no item number, rows sorted by their own cells) and
  `accuracy/score.json` (`jsonio.dumps(accuracy, sort_keys=True)`), only when a labelling is attested;
- `accuracy/attempts.json`, for every format-3 pack: every labelling of the cycle, oldest first, keyless
  (`labelling_id`, `drawn_by`, `labelled_by` and their names, `drawn_at`, `labelled_at`, the four sizes,
  `truth_source`, `truth_as_of`, `names_side`, `unlocatable`, `salt`, `selection_sha256`, `sheet_sha256`,
  `labels_sha256`, `score_sha256`, `counts` (the selection's) and `labels`, each population's label
  counts, null until it is labelled).

The pack adds no key, and its labels carry neither the sheet's item numbers nor its order: the sheet's
rows run in the order of their keys' salted digests, and the pack holds the salt (attests, the
`accuracy.draw` row), so either would put each label back on a masked key the reader holds. **What
remains:** PACK §2.3's "Beyond retention" gains the `key.view` rows labellers create, and a reader who
holds candidate masked keys (those rows, the adjudication sheet) can hash them with the salt and, with
`selection_sha256`, confirm which were drawn, with their population, root cause and `selected_for`. A
drawn key's label then follows wherever the labels file's rows of that population, root cause and
`selected_for` all carry one label. The pack does not prevent this: the salt and `selection_sha256` commit
the draw in the audit trail.

**Manifest:** `accuracy: {attested, draws, labelled}`: whether a labelling is attested (a boolean; the
attested one is `facts/attests.json`'s), and the cycle's labellings drawn and labelled (`attests.labellings`'
counts).

**Certificate** section "Labelled accuracy (observed)", after "Adjudication (observed)":
- `accuracy.lines` and `LIMITS`;
- the hand check: `To check by hand: sha256sum accuracy/labels.csv accuracy/score.json prints facts/attests.json's accuracy.labels_sha256 and accuracy.score_sha256; row N of audit/chain.tsv is the accuracy.label row of V-… holding both`;
- the warnings, when they apply:
  - `3 LABELLINGS OF THIS CYCLE WERE DRAWN (2 LABELLED); THE NEWEST A PERSON LABELLED IS ATTESTED`
  - `THE LABELLER STARTED RUN R-…: THE LABELS ARE NOT INDEPENDENT OF THE RUN` (or `WROTE SPEC APPROVAL A-…`)
  - `12 OF 120 SAMPLED DISCREPANCIES ARE UNDECIDABLE, 5 OF THEM NOT REVIEWED` (at 5% or more, or any
    `NOT_REVIEWED`)
  - `THE TRUTH SOURCE NAMES SIDE A (LEGACY): LABELS RESTING ON A COMPARED SIDE ARE CIRCULAR AND MEASURE NOTHING`
  - `THE KEY IS TOKENISED AND NO PATH WAS SHOWN IN CLEAR: LABELLERS COULD NOT FIND RECORDS IN THE TRUTH SOURCE`

With none attested and no labelling labelled, the section reads `No labelled accuracy is attested: which
side is right was not measured.` and there is no warning. With none attested and a labelling labelled (by
a token or in developer mode: a person's would be attested) it never says that nothing was measured: it
reads `No labelled accuracy is attested: 2 labellings of this cycle were drawn (1 labelled), none by a
signed-in person;
accuracy/attempts.json lists each with its label counts.` (`1 labelling of this cycle was drawn (1
labelled)`), and the certificate warns `2 LABELLINGS OF THIS CYCLE WERE DRAWN (1 LABELLED); NONE WAS
LABELLED BY A SIGNED-IN PERSON AND NONE IS ATTESTED`. The counts are `attests.labellings`'.

**`ds packs verify`.** `pack_verify._labelled(files, rows)` prints a line after `_drawn`, from
`attests.accuracy` (bound by step 5), finding the chain row whose action is `accuracy.label` and object the
attested id: `step 5 labelled accuracy: ok (row 81230 is labelling V-…'s accuracy.label row and holds the
SHA-256 of accuracy/labels.csv and accuracy/score.json, as facts/attests.json does)`, or `FAILED
(accuracy/labels.csv is missing or does not hash to facts/attests.json's accuracy.labels_sha256)`, `FAILED
(accuracy/score.json is missing or does not hash to facts/attests.json's accuracy.score_sha256)`, `FAILED
(audit/chain.tsv holds no accuracy.label row of labelling V-…)`, `FAILED (row 81230 does not hold
facts/attests.json's accuracy.labels_sha256 and score_sha256)`, or `FAILED (facts/attests.json's accuracy
cannot be read)` when the file does not parse under a manifest whose `accuracy.attested` is true, or the
labelling id or a hash is not text. A pack attesting no labelling has no such line. SAMPLING §11.4's
surrogate rule applies: a crafted file fails the line and exits 1, never 3. `_decided`, VERIFY.txt,
`PROVES` and `PROVES_SAMPLED` are unchanged. The raw-value gate searches the new files as CSV and JSON.
`rawvalues._GENERATED` gains `V` after the notifier's `M` and `N`: `(?:[ADJMNPRV]|LI)-[0-9A-Z]{26}`.

## 10. Interfaces

**API** (new `api/routes/labellings.py`, in `api/app.py`'s router tuple before `runs`; no `pack.sso`).
Refusals raise adjudicate's `AdjudicationError` (422), `AdjudicationConflict` (409) and `UploadTooLarge`
(413), which `_HANDLERS` maps already; `accuracy.DrawTimeout` gets a 504 handler; LookupError is 404.

| Route | Role | Answer |
|---|---|---|
| `POST /v1/runs/{run_id}/cycles/{cycle}/labellings` `{size?, per_cause?, accepted?, agreed?, truth, acknowledge_unlocatable?}` | operator | 201 `{labelling_id, salt, items: [{item, key, population, root_cause_id}], warnings}`; 200 the same for a repeat |
| `GET /v1/runs/{run_id}/cycles/{cycle}/labellings` | viewer | `{items}`, newest first: list items |
| `GET /v1/labellings/{id}` | viewer | the item: the row with `selection` counts, `accuracy`, `summary`, `lines`, `limits`, `attested` |
| `GET /v1/labellings/{id}/sheet.csv` | viewer | `text/csv`, `V-….csv` |
| `POST /v1/labellings/{id}/labels` (`text/csv`) | operator | 200, the labelled item |

`{cycle}` is 1 to 2,147,483,647, the cycles the store can hold; any other answers 422.

- **A list item** (`accuracy.listed`, from `repo.list_labellings`, which reads no sheet, of a selection
  only its three `drawn` counts (`item_count`) and, of a score, only what `summary` states: no root
  cause's or rule's figures) carries ids, actors, dates, sizes, truth and hashes only:
  `labelling_id run_id cycle drawn_by drawn_by_name drawn_at labelled_by labelled_by_name labelled_at size
  per_cause accepted agreed item_count truth_source truth_as_of salt selection_sha256 sheet_sha256
  labels_sha256 score_sha256 purged_at`, with `summary` and `attested` (the requested or approved sign-off
  attesting it, or null). `GET /v1/runs/{id}` `cycles[]` (`runs._evidence`), which the Run screen polls,
  gain `labellings` (list items).
- **The item** (`accuracy.detailed`) is the row without its sheet and its selection's `items`, with
  `cycle_drawn` and `cycle_labelled`, `summary`, `lines`, `limits` and `attested`: the selection's counts
  and the score (every root cause and rule) are read here only.
- `truth` missing its `source` or `as_of` gets their 422.
- **Purged cycles:** the draw, `sheet.csv` and the upload answer 410 (`CyclePurged`); the list and
  `cycles[]` show the tombstone with `purged_at` and `summary`, the item with `accuracy` and `lines` too.
- Scripts use an operator token.

**CLI.** `ds labellings [--run R-…] [--json]` is read-only (no `DS_OPERATOR`, no audit row): one
`accuracy.summary` line per labelling, newest first, of every run or of one (`repo.list_labellings` takes
an optional run); `--json` prints the API's list items (no selection, no score). An unknown run exits 2
(`run R-… not found`); no labelling prints nothing.

**UI.** `Run.tsx` gains a "Labelled accuracy" block per ended cycle, in every auth mode: the cycle's
labellings, each with the API's `summary`, Download sheet (until the purge) and Upload labels (an
operator, unlabelled, unfrozen); Draw (an operator, unfrozen: sizes, truth source and date, the hint, and
the acknowledgement tick box once the server's 422 asks for it), whose answer shows the API's warnings and
the drawn items linked to the Key screen (the draw's answer is the only place the API lists them; later
they are the sheet's); then the API's `lines` and `limits` of the labelling the reports state (`GET
/v1/labellings/{id}`). `Approvals.tsx` shows a format-3 sign-off's `review.lines`, the attested
labelling's `limits` (`GET /v1/labellings/{id}`) and the other labellings' `summary` (the cycle's list),
or `No labelling a person labelled is attested.` `web/src/accuracy.ts` holds the block's gating
(`takesLabelling`, `takesLabels`, `needsAcknowledgement`, and `stated`, which labelling the block prints)
and §6.3's hint (`DRAW_HINT`), no result text; `api.ts` gains `Labelling` (a list item), `LabellingDetail`
(the item), `LabellingDraw` and `DrawnLabelling`. The screens read a score or a selection from the item
only, which the Run screen reads again when the cycle's labellings change.

**Reports.** `reports/data.accuracy_section(data)` follows `strata_section` in `pdf.report_document` and
`summary.summary_document` as the section "Labelled accuracy (observed)": `lines` and `LIMITS`, or the
not-measured text (`accuracy.not_measured`, §11's report text 4, which counts the labellings drawn).
`RunData` gains `labelling`, `labelling_attested` (the attesting sign-off's id, or None) and `labellings`
(without a labelled one: how many were drawn). `load_run` reads them through `accuracy.reported(conn,
run_id, cycle, signoffs)`: the labelling the requested or approved sign-off's `attests.accuracy` names,
else `repo.attested_labelling(…, lock=False)` (a person's, unattested), else the newest labelled one. So a
report never says nothing was measured while a labelling of the cycle is labelled, whoever labelled it,
and the stated labelling's second line counts the cycle's others (§7). Live cycles (no `ds.cycles` row)
are not read and omit the section.

**Agent.** `agent/tools._run_summary` adds `accuracy`: null, or `{accuracy, summary, attested}` of the
labelling `accuracy.reported` gives for the run's newest ended cycle (the current one once ended, else the
one before, as `sample`):
- the stored block, cut so that a turn fits the profile's budgets (as the summary's root causes are cut):
  `by_cause` to its first 10 entries, in rank order, and `by_rule` to the 10 rules with most labelled keys
  (then most keys, then by rule id), each with the number left out (`by_cause_more`, `by_rule_more`); the
  tool's description says so;
- `accuracy.summary(…, named=False)`'s line, which says what kind of actor labelled (`by a person`,
  `through a token`, `in developer mode`) and never who: no actor's name or id reaches the model (AUTH's
  "no identity in prompts" stands; the stored block holds none);
- the attesting sign-off's id.

**Notifications.** `accuracy.measured` (ALERTS §2.4) is built here: one `EVENTS` row from `accuracy.label`, a run
event with facts `{cycle, labelling_id, labelled, undecidable, not_reviewed}`, severity info, dedup
`accuracy.measured|V`; the fact id pattern gains `V`; one fact model and adversarial test. `accuracy.label`
stays the audit action's name only: ALERTS had reserved it as a placeholder for the event, and
`core/notify.RESERVED` drops it. The row's object is the labelling, so the feed reads its run from
`detail.run_id` (`runner/notify._seen`) and its cycle from `detail.cycle` (`core/notify.concerned`). Text:
`customer-profile: labelling V-… of cycle 2 of run R-… was labelled: 150 items labelled, 4 undecidable, 1
of them not reviewed (observed).`

## 11. Messages (binding; each asserted)

**Draw:**
- 404: `run R-… not found`.
- 409: `cycle 2 of run R-… has not ended`; `cycle 2 of run R-… has a sign-off (A-…): revoke it before a
  new labelling`; `cycle 2 of run R-… has no discrepant item to label: set size and per_cause to 0` (the
  accepted and agreed forms read `no accepted key … set accepted to 0` and likewise; several, each whole,
  are joined by `; `); `cycle 2 of run R-… has no root cause to cover: set size above 0` (`per_cause`
  alone, and no discrepant item has a root cause).
- 410: the purge text (also for a purge that begins between the draw's two statements, §3 step 6).
- 422 (a `{cycle}` the store cannot hold gets the framework's 422, with no text of ours), in the order
  checked: the first five before the run is read, the sixth once it is found, the seventh after the
  cycle's sign-off 409 and the repeat, the last once the items are drawn:
  - `size must be 0 to 1,000, per_cause 0 to 20, accepted and agreed 0 to 500`
  - `a labelling draws at least one item: set size, per_cause, accepted or agreed above 0`
  - `a labelling holds at most 1,000 items: size, accepted and agreed ask for 1,200`
  - `truth.source must be a system identifier: 1 to 64 letters, digits and _ . : / -, starting with a letter`
  - `truth.as_of must be a date (YYYY-MM-DD) no later than today`
  - `truth.source holds a key-shaped value: name the system without customer keys`
  - `the key is tokenised (class account_number) and the spec shows no path in clear: labellers cannot find records in the truth source until the per-key reveal (AUTH §11) exists; set acknowledge_unlocatable to draw for labels a business rule decides without the record (docs/mvp/ACCURACY.md §4)`
  - `the sheet of this labelling would be over 512 KiB, leaving too little room for notes: draw fewer items`
- 504: `the draw of cycle 2 of run R-… was stopped after 300 s (QueryCanceled): nothing was drawn; draw again with accepted and agreed at 0, or when the store is less busy`
- Repeat warning (200): `labelling V-… with these parameters was drawn by you at 10:02:11 UTC and is returned, not drawn again`.

**Upload:**
- 404: `labelling V-… not found`.
- 409: `labelling V-… is already labelled: draw a new sheet`, or the sign-off 409 above. 410: the purge text.
- 413: `the labelled sheet is over 1 MiB: empty the notes column and upload it again`.
- 422, in this order:
  1. `the labelled sheet is not UTF-8 text (a spreadsheet's plain 'CSV' export writes Windows-1252): save it as 'CSV UTF-8' and upload it again` (or `the labelled sheet cannot be read as a CSV sheet: <reason>`, the reason being `the labelled sheet is empty`, `the labelled sheet line 7: more cells than header columns` or what the CSV reader refuses, `the labelled sheet line 7: <its error>`)
  2. `the labelled sheet names column(s) label, basis more than once` (header cells left empty name no column and are not read)
  3. `the labelled sheet lacks column(s) label, basis`
  4. `the sheet repeats item(s): each may appear once (lines 7, 9)`
  5. `row(s) for items the labelling did not draw (lines 4)`
  6. `every drawn item needs a label: mark UNDECIDABLE with a reason where the truth cannot be established, with reason NOT_REVIEWED where nobody reviewed it (items 12, 13)` (also what a quote left open in a note gives: it swallows the rows after it)
  7. `engine columns differ from the labelling's items (lines 5) (import the key column as text: a spreadsheet drops leading zeros)`
  8. `label must be one of A_RIGHT, B_RIGHT, BOTH_WRONG, BOTH_RIGHT, UNDECIDABLE (lines 4, 9)`
  9. `basis must be one of SYSTEM_OF_RECORD, SOURCE_DOCUMENT, BUSINESS_RULE, OWNER_CONFIRMED for a decided label, and empty for UNDECIDABLE (lines 3)`
  10. `reason must be one of NOT_LOCATABLE, TOKENISED, NO_TRUTH, TRUTH_CONFLICTS, CHANGED_SINCE, NOT_REVIEWED for UNDECIDABLE, and empty otherwise (lines 6)`

**Report texts** (the first line of `lines`, or the whole section):
1. `Labelling V-… drawn 2026-10-02 by amy, labelled 2026-10-04 by bob against CBS-PROD.account_master as of 2026-10-01, attested by sign-off A-….`
2. `Labelling V-… drawn 2026-10-02 by amy, labelled 2026-10-04 by bob against CBS-PROD.account_master as of 2026-10-01; no sign-off attests it.` (none requested yet, or revoked)
3. `Labelling V-… drawn 2026-10-02 by amy, labelled 2026-10-04 through token ci-bot (T-…) against CBS-PROD.account_master as of 2026-10-01, not by a signed-in person: no sign-off attests it.` (developer mode: `in developer mode (dev:loopback)`; any other actor that is no person: `by <name> (<actor>)`)
4. `Not measured: no labelled sample of this cycle.` (with labellings drawn and none labelled: `Not measured: no labelled sample of this cycle. 2 labellings were drawn, none labelled.`, or `1 labelling was drawn, none labelled.`)

The second line of `lines`, when the cycle has more than one labelling: `Labelling V-… is one of 3 drawn
for cycle 2 (2 labelled).`

**Certificate, none attested** (§9): `No labelled accuracy is attested: which side is right was not
measured.`, or `No labelled accuracy is attested: 2 labellings of this cycle were drawn (1 labelled), none
by a signed-in person; accuracy/attempts.json lists each with its label counts.`

**Pack (409):** `the attested labelling V-… does not hash to its recorded labels_sha256 and score_sha256: no
pack` (also when the segment's `accuracy.label` row of it does not hold both).

**Four-eyes (409):** `approval needs an approver other than you: you labelled its labelling V-…`.

## 12. Audit, privacy, retention, licence

**Audit** (AUTH §6, RETENTION §8), with ids, hashes and counts only, never a key, masked or raw:
- `accuracy.draw` (object `V-…`): `{run_id, cycle, salt, truth, size, per_cause, accepted, agreed,
  unlocatable, item_count, counts (the items drawn per population), selection_sha256}`;
- `accuracy.label`: `{run_id, cycle, sheet_sha256, labels_sha256, score_sha256, labelled, undecidable,
  not_reviewed}`.

A repeat audits nothing. Sheet downloads are not audited.

**Privacy.** Notes are never stored, hashed, audited or packed: the upload itself is not hashed, since
with the kept sheet at hand (which any viewer downloads) a hash of the upload is a hash of its notes, and
a short note could be confirmed by guessing. Outside the sheet (masked keys), everything holds counts,
ids, enumerations, dates, masked patterns and rule ids. A pack's labels carry no key, item number or sheet
order; what a pack reader who holds masked keys can still confirm is stated in §9. No actor's name or id
reaches the agent's model (§10).

**Retention.** `repo.finish_purge` (RETENTION §5.3 step 3), which erasure also runs, adds `UPDATE
ds.labellings SET purged_at = now(), sheet = NULL, selection = jsonb_set(selection, '{items}', 'null')
WHERE run_id = %s AND cycle = %s AND purged_at IS NULL`. Hashes, counts, truth and `accuracy` stay, and
rows are never deleted.

**Licence.** No check anywhere (O2). LICENCE §1 says so.

## 13. Migration `013_accuracy.sql`

```sql
-- Labelled accuracy (docs/mvp/ACCURACY.md): a salted sample of an ended cycle's items, labelled once
-- against a named truth source; frozen by the cycle's sign-off, tombstoned by its purge.
CREATE TABLE ds.labellings (
    labelling_id     text PRIMARY KEY CHECK (labelling_id ~ '^V-[0-9A-Z]{26}$'),
    run_id           text NOT NULL,
    cycle            integer NOT NULL,
    salt             text NOT NULL CHECK (salt ~ '^[0-9a-f]{32}$' AND salt ~ '[a-f]'),
    size             integer NOT NULL CHECK (size BETWEEN 0 AND 1000),
    per_cause        integer NOT NULL CHECK (per_cause BETWEEN 0 AND 20),
    accepted         integer NOT NULL CHECK (accepted BETWEEN 0 AND 500),
    agreed           integer NOT NULL CHECK (agreed BETWEEN 0 AND 500),
    truth_source     text NOT NULL CHECK (truth_source ~ '^[A-Za-z][A-Za-z0-9_.:/-]{0,63}$'),
    truth_as_of      date NOT NULL,
    selection        jsonb NOT NULL CHECK (jsonb_typeof(selection) = 'object'),
    selection_sha256 text NOT NULL CHECK (selection_sha256 ~ '^[0-9a-f]{64}$'),
    sheet            text,
    drawn_by         text NOT NULL,
    drawn_at         timestamptz NOT NULL DEFAULT now(),
    sheet_sha256     text CHECK (sheet_sha256 ~ '^[0-9a-f]{64}$'),
    labels_sha256    text CHECK (labels_sha256 ~ '^[0-9a-f]{64}$'),
    accuracy         jsonb CHECK (jsonb_typeof(accuracy) = 'object'),
    score_sha256     text CHECK (score_sha256 ~ '^[0-9a-f]{64}$'),
    labelled_by      text,
    labelled_at      timestamptz,
    purged_at        timestamptz,
    FOREIGN KEY (run_id, cycle) REFERENCES ds.cycles (run_id, cycle),
    CHECK (size + per_cause + accepted + agreed > 0 AND size + accepted + agreed <= 1000),
    CHECK ((sheet IS NULL) = (purged_at IS NOT NULL)),
    CHECK (num_nulls(sheet_sha256, labels_sha256, accuracy, score_sha256, labelled_by, labelled_at) IN (0, 6))
);
CREATE INDEX labellings_cycle_idx ON ds.labellings (run_id, cycle, labelling_id);

CREATE FUNCTION ds.tg_labellings() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_fixed text[] := ARRAY['labelling_id', 'run_id', 'cycle', 'salt', 'size', 'per_cause', 'accepted',
                            'agreed', 'truth_source', 'truth_as_of', 'selection', 'selection_sha256',
                            'drawn_by', 'drawn_at'];
    v_signoff text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'ds.labellings: labelling % cannot be deleted', OLD.labelling_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.purged_at IS NULL AND NEW.purged_at IS NOT NULL THEN
        IF (to_jsonb(NEW) - 'purged_at' - 'sheet' - 'selection')
               IS DISTINCT FROM (to_jsonb(OLD) - 'purged_at' - 'sheet' - 'selection')
           OR NEW.selection IS DISTINCT FROM jsonb_set(OLD.selection, '{items}', 'null') THEN
            RAISE EXCEPTION 'ds.labellings: the purge of labelling % keeps its hashes, counts and accuracy',
                    OLD.labelling_id USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF num_nonnulls(NEW.accuracy, NEW.labelled_at, NEW.purged_at) > 0 THEN
            RAISE EXCEPTION 'ds.labellings: labelling % must start unlabelled', NEW.labelling_id
                USING ERRCODE = 'check_violation';
        END IF;
    ELSIF OLD.labelled_at IS NOT NULL OR OLD.purged_at IS NOT NULL OR NEW.labelled_at IS NULL
          OR NEW.purged_at IS NOT NULL
          OR (SELECT bool_or(to_jsonb(NEW) -> f IS DISTINCT FROM to_jsonb(OLD) -> f) FROM unnest(v_fixed) f) THEN
        RAISE EXCEPTION 'ds.labellings: labelling % is labelled once, then only purged', OLD.labelling_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    PERFORM 1 FROM ds.cycles WHERE run_id = NEW.run_id AND cycle = NEW.cycle FOR UPDATE;
    SELECT approval_id INTO v_signoff FROM ds.approvals
     WHERE kind = 'signoff' AND run_id = NEW.run_id AND cycle = NEW.cycle AND status IN ('requested', 'approved')
     LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION 'ds.labellings: cycle % of run % has a sign-off (%)', NEW.cycle, NEW.run_id, v_signoff
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER labellings_lifecycle BEFORE INSERT OR UPDATE OR DELETE ON ds.labellings
    FOR EACH ROW EXECUTE FUNCTION ds.tg_labellings();
CREATE TRIGGER labellings_no_truncate BEFORE TRUNCATE ON ds.labellings
    FOR EACH STATEMENT EXECUTE FUNCTION ds.append_only();

-- A sign-off's exclusion set also holds whoever labelled the labelling it attests (§4).
CREATE OR REPLACE FUNCTION ds.approval_authors(a ds.approvals, OUT persons text[], OUT unattributed boolean)
LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF a.kind = 'spec' THEN
        SELECT s.persons, s.unattributed INTO persons, unattributed FROM ds.spec_accountable(a.spec_id) s;
    ELSE
        SELECT coalesce(p.authors, '{}') || ds.accountable(r.created_by) || ds.accountable(j.scored_by)
                   || ds.accountable(v.labelled_by)
                   || ARRAY(SELECT x FROM jsonb_array_elements(coalesce(a.attests->'sample'->'ledger', '[]')) l,
                                   unnest(ds.accountable(l->>'closed_by')) x),
               p.unattributed IS NOT FALSE OR NOT ds.attributed(r.created_by)
          INTO persons, unattributed
          FROM ds.runs r LEFT JOIN ds.approvals p ON p.approval_id = a.attests->>'spec_approval_id'
          LEFT JOIN ds.adjudications j ON j.adjudication_id = a.attests->'adjudication'->>'adjudication_id'
                                      AND j.run_id = a.run_id AND j.cycle = a.cycle
          LEFT JOIN ds.labellings v ON v.labelling_id = a.attests->'accuracy'->>'labelling_id'
                                   AND v.run_id = a.run_id AND v.cycle = a.cycle
         WHERE r.run_id = a.run_id;
    END IF;
    persons := ARRAY(SELECT DISTINCT x FROM unnest(persons || ds.accountable(a.requested_by)) AS x ORDER BY x);
    unattributed := unattributed IS NOT FALSE OR NOT ds.attributed(a.requested_by);
END $$;
```

- **`approval_authors`.** The replacement extends 011's (012 leaves it). `ds.accountable(NULL)` is `{}`, so
  earlier sign-offs keep their sets.
- **Upgrade.** No rewrite and no scan. Sign-offs requested before 013 stay format 2. `control-plane.sql`
  gains a note of the table, as 011's and 012's tables have: the target schema has no `ds.cycles` or
  `ds.approvals` for it to reference.
- **repo** gains `labelling_draw`, `labelling_items`, `insert_labelling`, `repeated_labelling`,
  `get_labelling` (with names, roles, the cycle's labellings counted and independence),
  `list_labellings(…, *, full=False)` (the list's projection, §10; `full`: rows as `get_labelling`'s),
  `label_labelling`, `attested_labelling(…, *, lock=True)` and `cycle_labelling`, a shared `_BUG` fragment
  (from `stratum_counts`, naming the run and cycle), and extends `finish_purge` and `replace_root_causes`
  (§3 step 5). `store/db.py` names the migration `ACCURACY`.

## 14. Seams

**Notifications:** `accuracy.measured` is built here (§10), from `accuracy.label`; the notifier's code changes only by its
projector, model, text, dedup key, `concerned` and the run `_seen` reads for a labelling's row.
**Readiness** reads the attested, else newest labelled, `accuracy` (`deltasleuth-labelled/1`) per pair and cycle;
its gate reads `discrepant.sides.b.wrong`, whose name and meaning stay; it keeps the population text, pools
nothing and owns any gate. **Benchmark:** the harness times no draw (its cost is held by the `DS_PERF`
marks, §16), but it sees §3 step 5's analysis: `repo.replace_root_causes` may analyse the findings during
the cycle end, which `tools/bench.py` (`_analysed`) counts as `analysed`, not as `analysed during the
cycle end`, unless autovacuum analysed them there too (BENCHMARK §7).

## 15. Documents to amend

- **ALERTS:** §2.1 (the `accuracy.measured` row; a labelling's row names its run in its detail), §2.3
  (ids gain `V`), §2.4 (no longer reserved), §6.2 (its wording), §10 (the reserved-event message), §12 (`_GENERATED` gains `V`), §16.
- **APPROVALS:** §1.2 (format 3: `labellings` and `accuracy`), §3 (labeller excluded), §6.1 (review),
  §6.2 (`<why>`), §6.3 (the review screen), §8 items 1 (formats 1, 2 and 3) and 6, §12.
- **PACK:** §1.1 (format 3, refusal P3); §2.1 (files, the labels file's columns, `attempts.json`); §2.2
  (manifest, certificate, its texts and warning when none is attested); §2.3 (`V-` ids; label words under
  loose patterns; no item number or sheet order; what a reader holding masked keys can confirm of the
  labels); §3.3 (`ds packs verify` line); §4.3 (`V-` objects); §7.2; §7.3; §10.
- **RETENTION:** §4.2 (sheet and items expire; no note and no hash of the upload is kept; what a
  downloaded pack keeps of the labels), §5.3 step 3 (`finish_purge`), §8, §11, §12.
- **AUTH:** §4.1 (routes, the 409s and the 504), §4.2 (`ds labellings`, local, no audit, no
  `DS_OPERATOR`), §4.3 (no actor's name or id reaches the model), §6 (the two actions' details; no hash
  of the upload), §11 (the reveal as this feature's dependency).
- **SAMPLING:** §11.5 (a labelling reads no stratum label; the reader list is unchanged).
- **LICENCE:** §1.
- **CONTRACTS:** §1, §3 (the table, the store's rules), §5, §6 (the agent's block, its cut and its line),
  §7 (routes, list items, `cycles[].labellings`, the 410s), §8, §10.
- **BENCHMARK:** §7 (the findings' analysis while the cycle end links root causes counts as `analysed`,
  §14).
- **RUNBOOK:** new "Labelling against the truth" after "Reviewing the adjudication sheet": sizing, the
  truth and circularity, locating records and the acknowledgement, basis, `observed_at` and `as_of`,
  several findings in one item, `NOT_REVIEWED`, notes, UTF-8 and leading zeros, a repeat (after a proxy's
  timeout or a dropped connection the first labelling comes back) and DeltaSleuth's own 504 (nothing was
  drawn: draw again), labelling before the sign-off request, drawing once (a labelling superseded by a
  new draw shows, and a token's labelling is counted, never hidden), what the pack carries; the roles
  table, the sign-off paragraph, what a purge keeps, the notification events and the troubleshooting
  rows.
- **architecture.md:** §2.3 ("DeltaSleuth still does not decide which side is correct; labelled accuracy counts a
  person's labels against a named truth source, observed"); §10; §15 (informs, never gates).
- **README, PLAN:** the feature in the MVP's list and scope, its deferred seams. **PENTEST:** label
  discipline and the labelling sheet in scope. **DEPLOY:** §3's list of what notifies; §8 (what the chat
  sends of a labelled cycle: its stored counts, shares and ids, and a line without the labeller's name or
  id). `control-plane.sql`: a note of the table (§13).

## 16. Tests

No image is pulled (local `postgres:16-alpine`).

| File | Behaviours |
|---|---|
| `core/test_sampling.py` | `test_interval_pinned` (§6.3, 1e-12); `test_interval_sizing` (§6.3's sizing); scipy grid (x 0–60, 34 sizes n from 1 to 500) and an exact integer oracle on both tails (the symmetry the lower end is computed by); `test_shown_contains_exact` (Hypothesis); `test_upper_default_unchanged`; `test_conservative_without_replacement` (N ≤ 40, minimum 0.97134); `test_without_replacement_spot_checks` (every D at N 1,000 and 5,000, n 50 and 120: at least 0.95) |
| `reports/test_accuracy.py` | `test_worked_example` (every §6.3 string, the labels' worst case against scipy, 733 to 967, 1 to 297); `test_lines_golden` (§7 verbatim, and the warnings); the census, no-undecidable, undecidable-census and many-causes forms with the `GET /v1/labellings/V-…` pointer; `test_summary_golden` (and `named=False`: no actor); `test_never_stated` over `lines` and `summary`; `BOTH_RIGHT` never in `wrong`; `worst_case` of `b_share_of_single_side_right`; `NOT_REVIEWED` counted apart; `names_side` (target, table `name`, `schema.name`, target prefix, case); the `as_of` warning; uniform = `sorted(digest)[:size]`; a cause's items are its bottom m_h; census exact; `*` for a masked NOT_FOUND; §2.1's populations; chi-square inclusion over 1,000 salts; `tally(keyless) == score`; §11's draw and upload messages (the 504's and the repeat's are in `store/test_labelling_draw.py`, the four-eyes and the pack 409s in `api/test_labellings.py` and `reports/test_pack_accuracy.py`), a repeated header column and a quote left open among them, each upload check in §11's order; what the CSV reader refuses is a 422; a refused upload changes nothing; `test_labels_are_stored_once_without_notes_and_the_score_is_re_derived` (canary raw key and value, a note over 131,072 characters, no hash of the upload, keyless rows sorted with no item number); `test_a_stored_labelling_prints_its_lines_in_the_golden_order` (§7's lines from a row read back from the store); `test_a_key_is_unlocatable_when_no_path_resolves_clear`; the list's rows read no sheet, selection or cause figures; the second line counting the cycle's labellings |
| `store/test_labelling_draw.py` | SQL equals Python over 10,000 synthetic results beside another run of the same keys with other results, with `*` items (a key with root-caused and INFO or suppressed findings gets only its causes) and non-ASCII `business_code` keys; a new store's first cycle (analysed before its root causes are linked) is drawn without a nested loop, and `replace_root_causes` analyses its links, once; coverage in `seq` order stops at 1,000 with `uncovered`; a timeout is `DrawTimeout` and stores nothing; a repeat, sequential or racing, returns the first; `DS_PERF`, seeded in the cycle end's order (`analyse_new`, then `replace_root_causes`, nothing analysed by hand) beside another run a tenth its size: 1M results drawn in 10 s (measured: 4.7 s), 10M in 120 s (**unmeasured**: its store needs about 20 GB, and the mark was not run) |
| `store/test_migration_013.py` | over a populated 012 store: every trigger refusal (delete, truncate, second label, fixed columns, purge beyond `items`, frozen under a requested sign-off); `approval_authors` keeps scorer and closers and adds the labeller |
| `test_approvals.py` | format 3 stable across a purge; formats 0–2 rebuilt; token and developer labellings never attested, and counted (`labellings`) with `accuracy` null; a person's attested over a newer unlabelled draw; a labelling never attested as an adjudication, nor an adjudication as a labelling (`test_a_sign_off_attests_the_newest_labelling_a_person_labelled`) |
| `api/test_labellings.py` | roles; tokens and developer modes; a draw or upload racing a sign-off request gets 409; 404/409/410/413/422/504; a `{cycle}` out of range 422; the acknowledgement; a repeat's 200; a list item's and `cycles[]`'s keys (no selection, no score); purged: draw, `sheet.csv` and upload 410, list and `cycles[]` tombstones with `summary`, the item's with `accuracy`; a purge between the draw's two statements 410; `lines` and `limits` returned; warnings; `_why` (`you labelled its labelling V-…`) |
| `api/test_roles.py` | `ROUTES` gains the five routes (operator, viewer) |
| `api/test_audit.py` | `test_accuracy_audit_holds_no_key`: `accuracy.draw` and `accuracy.label` details hold their members only: no drawn key, masked or raw, no note and no hash of the upload; a sheet download audits nothing |
| `api/test_label_canaries.py` | no sign-off stratum in any labelling output of a sampled cycle |
| `reports/test_pack_accuracy.py`, `test_pack_tamper.py` | files, manifest, certificate (`LIMITS` verbatim, hand check, each warning); labels without item numbers, sorted, in another order than the sheet's; a labelling no person labelled counted on the certificate with its warning, and in `attests.labellings`; unlabelled draws not measured; `sh` procedure passes; the verify line's ok and every FAILED text; an edited `score.json` fails step 3 and, re-signed, the line; the integrity refusal; format-2 packs unchanged |
| `reports/test_adjudications.py` | the shared sheet reader: a repeated header column refused, a note over 131,072 characters read |
| `reports/test_pdf.py`, `test_summary.py`, `cli/test_cli.py`, `web/test/accuracy.test.ts` | the four report texts, in report.pdf and the P0 summary alike, a live cycle without the section, a person's labelling before a newer one no person labelled, the line counting the cycle's labellings; the summary's files printing `lines` and `LIMITS` verbatim; `ds labellings` (newest first, one run, `--json` as the API's list items, an unknown run, no audit row, no `DS_OPERATOR`); UI gating, the hint, the requests, and that no screen builds a result text |
| `agent/test_tools.py` | `get_run_summary`'s `accuracy`: null until labelled, the stored block cut to its first root causes with the rest counted, the rules with most labelled keys kept, no key or note, and no actor's name or id in the framed result |
| The notifier's event tests (`core/test_notify_events.py`, `runner/test_notify_feed.py`, `test_config.py`, `test_integration_contracts.py`) | `accuracy.measured`: fact model and adversarial projection; the feed reads a labelling's run from its row's detail; `accuracy.label` is no event name, and no longer reserved |
| `test_bench_tool.py` | the findings' own analysis during the cycle end counts as `analysed`, unless autovacuum analysed them there too (§14) |

## 17. Limits, deferred, size

**Limits.** One labeller of record; intervals exclude labelling error; coverage without replacement is
checked, not proven. Tokenised keys without a clear locator give mostly `UNDECIDABLE` rows. Circularity is
caught only when the source names a side. Redraws and run dependence are visible, not prevented. No frame
weighting; row-hash matches are never sampled. Per-cause intervals are wide. Label words trip a permissive
`key.pattern`, as sheet headers do. The draw is synchronous, bounded by 300 s; a repeat returns the first.
A pack reader who holds masked keys can confirm which were drawn and, where a class's labels are all one,
their label (§9). The unlocatable gate reads the policy's listed paths, not the documents: a path resolved
clear may still locate nothing. The 10M draw's budget is unmeasured (§16).

**Deferred:** the per-key reveal; double labelling; a Bonferroni whole-population bound; an exact
hypergeometric interval; frame weighting and a per-stratum view; browser labelling; a truth connector (a
declared, SSRF-guarded destination); a draw run by the worker.

**Size:** as built, about 1,950 Python product lines (`reports/accuracy.py` 1,010 of them), a 100-line
migration, 240 TypeScript and 3,500 test lines, with no new dependency.
