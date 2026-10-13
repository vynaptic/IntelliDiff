# Cut-over evidence packs: contract

This contract is binding. It builds evidence packs for census cycles, consumes [APPROVALS.md](APPROVALS.md) §8,
and delivers the hash-chained audit log that [AUTH.md](AUTH.md) §6, §11 and [RETENTION.md](RETENTION.md)
§11, §12 defer to it. Packs of sampled cycles are [SAMPLING.md](SAMPLING.md) §11, which amends §1,
§2, §3.4, §7 and §10. [ACCURACY.md](ACCURACY.md) §9 amends §1.1, §2, §3.3, §4.3, §7.2, §7.3 and §10 for
the labelling a format-3 sign-off attests. [READINESS.md](READINESS.md) amends §1.3 (the key census is
counted with `packs` or `readiness`), §2.3 and §4.3 (readiness never enters a pack).
[SCHEDULES.md](SCHEDULES.md) §7 amends §2.3 (a scheduled run's plan names its schedule; `SC-` ids are
generated text).

**Goal:** one ZIP per signed-off census or sampled cycle that an auditor checks offline, years later, with
stock tools and the site's key register: this installation's key issued it unchanged, who decided what, over
which population parity was observed. Aggregate evidence only; every claim is *observed*. Tokens and
developer modes have no sign-offs, so no packs.

## 1. What a pack certifies

### 1.1 Preconditions

`reports/pack.problems(ctx, conn, run, cycle)` returns every refusal (§7.2): (0) the licence at issue
([LICENCE.md](LICENCE.md) §6, D8), listed first: an Enterprise licence not expired (M4, M7), and a run not
made in an unlicensed evaluation (M8; a run without a licence record is judged by the licence at issue
alone). An open build, which embeds no vendor key, has no licence at issue and never raises M4 or M7:
of (0) it keeps only M8, for a run an older build that enforced the licence made in an evaluation
([LICENCE.md](LICENCE.md) §19); (1) oidc mode, `packs` configured; (2) the run was created after 009 (`repo.applied_at(conn, 9)`);
(3) an approved sign-off whose `attests` has `format: 1`, `2` or `3` and, rebuilt now by that format's
builder from its own `adjudication_id` (format 3: and its own `labelling_id`), hashes to its
`attests_sha256`; (4) `approvals.signoff_problems(conn, run,
cycle, spec, cited=attests["spec_approval_id"], later=False)` is empty (a sampled cycle's G1–G9 and G11; R-c
is read from the final grant, [SAMPLING.md](SAMPLING.md) §9); (5) `attests.census` counts side a in this
cycle (§1.2; a sampled cycle's frame record of side a, else P1; a side a read from files that held lines
that were not rows is no census, §1.2); (P2) a sampled cycle's `sha256(salt_label
":" nonce)` and `selection_sha256` equal those of the `sample.draw` row at chain seq
`attests.sample.draw_seq`, which must name the run and the draw (SAMPLING §11.1); (P3) a format-3
sign-off's attested labelling: its kept sheet's labels (`accuracy.keyless`) and its score hash to its
recorded `labels_sha256` and `score_sha256`, and its `accuracy.label` row in the segment holds both
([ACCURACY.md](ACCURACY.md) §9); (6) from
`adjudicate.EXIT_MINIMUM` (50) per-key results (new `repo.count_results(conn, run_id, cycle)`), a passed
`attests.adjudication` (D1); (7) the audit segment verifies (§4.3); (8) no raw key-shaped value in the pack
(§2.3); (9) no purge started (410). It builds the pack in memory, unsigned, to judge (7), (8) and its size,
once (2) holds and the sign-off is a fresh format 1, 2 or 3 (else it stops at those refusals: a table scan
started before 009 saved no snapshots or times); `issue` stores that same build.

### 1.2 Census, observed per side

`approvals.attests` format 1 adds `format`, `census` and `adjudication` (§5) to format 0 (008's) and
names its rule fields (`_RULE_FIELDS`) instead of `asdict(KnownDiffRule)`, so code changes need format 2
and never stale a signed cycle; `approvals.rebuilt(conn, run, cycle, spec, recorded)` rebuilds a sign-off
by its recorded format (none for a format this build does not know: refused as changed).
`runner/census.of_cycle(run, ended, scan, spec)` (`spec` names each side's target) gives `{cycle, at, a,
b}`, a record, a failed count (`{failed}`) or null per side:

| Basis | Side | Observed by | Record |
|---|---|---|---|
| `scan` | both | each table read to its end (`ds.scans.state.done`; graph specs also `state.graph`) | `{basis, target, relation, rows, skipped, snapshots, first_at, last_at}` |
| `listing` | `listing_side(source)` | `runner/keys._listed`, refusing incomplete, unstable or ambiguous walks | `{basis, target, items, first_at, last_at}` |
| `table` | table sides of key-list runs | §1.3 found `missing == 0` | `{basis, target, relation, where, rows, skipped, missing, extra, at}` |
| `frame` | the side a hash sample's `table` frame member reads | the draw read it to its end in one snapshot, or as its `consistency` says ([SAMPLING.md](SAMPLING.md) §3) | `{basis, target, relation, where, rows, skipped, keys, unfit, at}` |

A `scan`, `table` or `frame` record of a side on MySQL or Oracle adds `engine` and `consistency`
(`snapshot`, `unverified` or `none`: [ENGINES.md](ENGINES.md) §6, §11). Such a record of any table side
whose plan names its engine, PostgreSQL's in a pair with MySQL or Oracle too, adds `server`: the major
and minor version of the server the read met (a scan's last pass, a census, a frame; ENGINES §13). A
PostgreSQL pair's records are as above.

A hash sample's draw stores its frame's census through `repo.set_census`, a record per side a member
enumerates (a listing member's `listing` record adds `keys` and `unfit`), and `census.described` reads
`frame` as `its population read to its end in 1 snapshot at <t> as the sampling frame`. `census.pending`
and `repo._CENSUS_PENDING` leave out `keys_source = 'hash_sample'`: a key census would count every
unsampled key missing.

**Side a (the baseline) is required**; `role` is only a label. A count of side b cannot show keys side b
lost; its coverage is still printed. `explicit` or `list` keys observe no side, nor do a key table's
(`keys_source` `table`): its read's record, `{basis: "key_table", target, relation, rows, keys, at}`, is
audited with the run's creation and each roll and is never a census (`census.listed` takes `listing`
only), and between two services the pack is refused, `its keys are the spec's key table's and no side is
a table to check them against`. A scan cut by maxKeys
counts no side. A key-list run's records are stored on the run as its current cycle's census
(`repo.set_census` merges each into it; `at` is the latest), copied to `ds.cycles.census` when that cycle
ends: §1.3's, and a complete listing's, packs configured or not, for the sides whose target it reads,
written when `runner/keys.resolve` walks it (at creation, or a rolling boundary).

**Snapshots.** Resumed scans open fresh snapshots (TABLES §5), L2 its own. `scan` counts each side's
`_snapshot` openings (`state["snapshots_a"]`, `_b`, `first_at`, `last_at`), saved with the rows read in
them; `graph.analyse(ctx, spec, tables, state)` sets `state["graph_at"]`. The certificate prints `read to
its end in 1 snapshot at <t>`, or as `Document.warning` `SIDE A READ IN 3 SNAPSHOTS between <t1> and
<t2>: rows added behind a resume point meanwhile were not read`; a listing, `a paginated walk between <t1>
and <t2> (ordering declared stable by the target), not a snapshot` (`census.described`, `census.warning`;
a scan state saved before 009, without snapshots or times, reads `read to its end; snapshots not recorded
(before 009)`).

**A side its engine could not pin** ([ENGINES.md](ENGINES.md) §13). Where `in 1 snapshot` stands (a scan,
a key census, a frame), a record whose `consistency` is `unverified` reads `in 1 transaction, a snapshot
not observable (a MySQL view)` and one whose `consistency` is `none` reads `in 1 pass, not a snapshot (a
MySQL table outside InnoDB)` (`3 transactions`, `3 passes` for a resumed scan). Both add the warning `SIDE
A NOT READ IN A SNAPSHOT (<that reason>): rows written during the read may be missing or read twice`,
beside the resumed scan's (`SIDE A READ IN 3 TRANSACTIONS between ...`). The pack is still issued, as for
a listing's walk. Every record that names an `engine` also reads `; rows and values as the account reads
them (redaction, masking and row policies apply)`: the certificate says whose view of the rows was
compared.

**A side read from files** ([FILES.md](FILES.md) §11). A `scan`, `table` or `frame` record of a file side
holds no `snapshots` and no digest; it adds `engine: files`, `consistency: fixed`, `server` (DuckDB's
major and minor version), `files` (their count), `bytes`, `pinned_at`, `verified_at` (when the files
hashed the same at the read's end), `as_of` (what the stamp file declares, or null), `modified_last`,
`mount` (`read-only` or `writable`), `rejected` (lines of its files that were not rows), `lines_skipped`
(the lines its spec's `csv.skip` left unread) and `repeated` (keys several rows hold; null in a frame's
record, which counts none). Where `in 1 snapshot at <t>`
stands it reads `from 12 fixed file(s), 3,650,722,201 bytes, pinned 2026-10-01 02:14 UTC and hashed the
same when the read ended at 2026-10-01 02:20 UTC; an export, not the system it came from: declared as of
2026-09-30 23:59 UTC by its stamp file, not observed; newest file modified 2026-10-01 01:58 UTC on the
mount` (without a stamp file, `its own time is not observed`; after the bytes, where `csv.skip` left
lines unread, ` (24 line(s) at the files' starts skipped as the spec declares, not read)`), and never `;
rows and values as the account reads them`. The certificate alone adds, from the run's plan, `; sha256
<64 hex> of the list of its files' SHA-256 values, a line each, in the byte order of their paths under
the target's root, not a file's own (sha256sum FILE... | cut -c1-64 | sha256sum); reads of single keys
before and after it were checked by size, times and inode, not hashed again` (of a side that declares
`partitions`, `… of the list of its files' sha256sum lines, each with its path under the target's root, in
the byte order of those paths, not a file's own (sha256sum PATH... | sha256sum, run in that root); …`):
the record itself holds no digest, and the digest is never a file's own SHA-256, of a side of one file
neither, so the line says what it is and how the holder of the files prints it.
**Side a with `rejected > 0` is no census**, whatever its basis: a key on such a line
that side b lacks is in no result, the case the baseline's census exists to exclude, so the pack is
refused (§7.2). On side b the pack is issued with the warning `SIDE B: 3 LINE(S) OF ITS FILES WERE NOT
ROWS and were not compared: a key on such a line is not compared, and a key not found on this side is
inconclusive`; either side adds `SIDE A: 5 KEY(S) ARE HELD BY MORE THAN ONE ROW: each is inconclusive,
never paired` and `SIDE A WAS READ FROM A DIRECTORY DELTASLEUTH COULD WRITE TO` where they hold. A file
side has no several-snapshots warning: a resumed pass reads the same pinned bytes, and a changed file
fails the run before any pack.

**Own cycle.** Records carry their `cycle`; another cycle's refuses. Key-list runs re-count each cycle
(§1.3); a non-rolling listing does not re-walk, so only its first cycle gets a pack.

### 1.3 The key census check (`runner/census.record`)

With `packs` or `readiness` configured (`readiness: {}` suffices: [READINESS.md](READINESS.md) §4; the
worker's `counting`), the worker runs it first in each cycle of a key-list run with a table side,
once `cycle._rolled` fixed the keys, before leasing: `repo.lease_due(…, census=True)` leases no key of a
cycle `repo.census_due` still lists. Per table side, in one snapshot of its engine (`Engine.snapshot(target,
timeout_s=TIMEOUT_S)`, an hour a statement; PostgreSQL's is `pg.snapshot`), it streams the side's census
(`Engine.census`; PostgreSQL's `queries.census_sql(side)`: `_population`'s canonical key parts, key
maps and approved filter, in `scan_sql`'s order), counts `skipped` (`null_keys_sql`) and merges with the
sorted keys: `missing` counts population keys absent from the key set (keys failing `key.pattern`
included), `extra` keys outside it; a key several rows hold counts once, and `rows` counts every row of
the filtered table, `skipped` among them, as a scan's `rows_a` does. In one snapshot, churn cannot hide an
omitted key, nor an out-of-filter key stand in: that holds for a side whose `consistency` is `snapshot`
(every PostgreSQL and Oracle side, a MySQL InnoDB table) and for no other, which the certificate warns
of (§1.2). A file side's census (`fixed`) reads files that cannot change under it without failing the
run: its record says which bytes, hashed the same before and after the read, and nothing of the system
they were exported from; it also counts `repeated` and holds `lines_skipped`, the lines its spec's
`csv.skip` left unread, which its wording says; a changed file is recorded `{failed: FilesChanged}`, and a
read that met other rows than the plan counted fails the run (`RowsMoved`: [FILES.md](FILES.md) §7).

Audited `run.census` per side (`{cycle, side, basis, rows, missing, extra, snapshots}`, `snapshots` 1,
with `engine` and `consistency` when the record has them; a
failure `{cycle, side, basis, failed}`), it is stored by the new `repo.set_census(conn, run_id, census)`
and copied by `repo.end_cycle` to `ds.cycles.census` if its `cycle` is ending. A failure, timeouts
included, records `{failed: "<error class>"}` and is warned; the cycle goes on. The spec is compiled
without today's targets, so a side whose target left `targets.yaml` is recorded failed too
(`UnknownTargetError`), and its keys lease (the run then fails as it would without packs). Counted for
readiness alone, in tokens or developer mode, the census is recorded and audited the same way: a
key-list pair then reads its population gate from it, and no pack follows from it.
The run and `Run.tsx` show `census pending` or `census of side a failed (QueryCanceled): no pack will
cover side a`.

Under [PRECHECKS.md](PRECHECKS.md) §7 (V3) the census of a key-list cycle also waits for the cycle to
fall due and for the schema guard's look at that cycle's start, which tries twice at most (a side
unreachable is tried again a lease period later, then the look is recorded as not made and the census
goes on); a change that breaks the run's plan pauses the run before its census.

## 2. The pack

### 2.1 Layout

`P-<26>.zip` holds folder `P-<26>/`. Unlisted: `SHA256SUMS` (coreutils lines, sorted, ASCII paths),
`SHA256SUMS.sig` (DER ECDSA P-256/SHA-256, RFC 6979), `signing-key.pem` (SPKI). Listed: `VERIFY.txt`,
`manifest.json`, `certificate.pdf`, `csv/{root_causes,coverage,graph}.csv`, and:

| Path | Exact content | Bound to |
|---|---|---|
| `facts/attests.json` | `jsonio.dumps(attests, sort_keys=True)`, no newline | `attests_sha256` in the sign-off's final `approval.grant` |
| `facts/spec.json` | `assemble.content_hash`'s form of the body | `content_hash` in the spec approval's final grant |
| `facts/plan.json` | `jsonio.dumps(stored params without "approval", sort_keys=True)` | `plan_sha256` in `create_run` (§7.4) |
| `adjudication/{sheet.csv,score.json}` | the attested sheet, notes emptied, and score (§5) | `attests.adjudication` |
| `adjudication/attempts.json` | every draw of the cycle, scored or not | `adjudication.*` rows |
| `accuracy/{labels.csv,score.json}` (format 3, a labelling attested) | the attested labelling's kept sheet without its item numbers, keys, observation times and ids and notes (columns `population root_cause_id path_pattern change_type rules engine_outcome selected_for label basis reason`), its rows sorted by their own cells (`accuracy.keyless`), and its score (`jsonio.dumps(accuracy, sort_keys=True)`; [ACCURACY.md](ACCURACY.md) §9) | `attests.accuracy.labels_sha256`, `score_sha256`; its `accuracy.label` row |
| `accuracy/attempts.json` (format 3) | every labelling of the cycle, labelled or not, without keys | `accuracy.*` rows |
| `audit/chain.tsv` | `seq⇥prev⇥obj_prev⇥hash⇥object⇥text` per row, newline-terminated (a text may hold U+2028, U+2029 or U+0085 raw), verbatim: the run's `key.view` rows carry the masked key a viewer opened (§2.3) | the chain; the SIEM |
| `annex/ai_answers.json` | per answer of this cycle (`m.cycle`): `{created_at, cycle, key_masked, citations, content_sha256}`; `AI-generated, not evidence` | — |

**Unchanged by table pre-checks** ([PRECHECKS.md](PRECHECKS.md) §9): no file above holds a table pre-check or anything of
the schema guard's record (`ds.runs.guard`); a pack holds the guard's audit rows in its segment only (§2.3).

**Left out:** `report.pdf` and per-key CSVs (`discrepancies`, `retry`, `normalization`, `suppressed`;
rule key counts are in `attests.rules`), kept in the store until the cycle's retention date; model text
(deferred). The three CSVs are the store's exports without their key tokens (`write_csv(..., keyed=False)`):
`root_causes.csv` without `member_key_tok` and `sample_keys`, `graph.csv` without `label` and without
the masked members of its `detail` (`only_a`, `only_b`, `sample`); their counts stay. Over 100 MiB or
§7.3's zip limits refuses. The adjudication's sheet and score are there only when the sign-off attests
one; `attempts.json` lists each draw's ids, actors, times, limit, salt, candidates, unrepresented
classes, hashes, `passed`, `adjudicated` and `agreed`, never its keys. A format-3 sign-off's pack
carries `accuracy/attempts.json` (each labelling's id, actors and their names, times, sizes, truth source
and date, `names_side`, `unlocatable`, salt, `selection_sha256`, `sheet_sha256`, `labels_sha256`,
`score_sha256`, its selection's `counts` (the cycle's populations and the items drawn of each) and its
label counts per population, null until it is labelled; never its keys, and no hash of an upload), and
the two `accuracy/` files of the labelling it attests, if any; a format 1 or 2 sign-off's pack has none of
them. Entries are written in byte
order, deflated, but stored when a file would deflate over 50:1, so that no pack trips §7.3's 100:1.

### 2.2 Manifest and certificate

`manifest.json` (indent 2; `ds.packs.manifest`): `format: "deltasleuth-pack/1"`, `procedure:
"deltasleuth-pack/1 verification"`, `pack_id`, `installation`, `claim: "observed"`, `claim_text`,
`issued_at(_epoch)`, `issued_by`, `signing_key`, `subject` (run, cycle, spec name, version, content
hash, sides' targets and relations, and each table side's `engine` and `server`, its major and minor
version, when the plan has them: a pair with a MySQL or Oracle side; never a patch level, host, port or
user. `server` is the version the cycle's census record says its read met (§1.2), and the plan's, the
one the run was created on, when the record says none; a file side adds `files` `{count, bytes, sha256,
sha256_of, as_of}` from its plan: the count and bytes of its files, the one digest of them all
(`files_sha256`, [FILES.md](FILES.md) §6), what that digest is the SHA-256 of with the command that prints
it (the certificate's words, §1.2: step 4 of `VERIFY.txt` prints the subject, so the pack says there too
that the digest is no file's own) and the time its stamp file declares, null without one), `census`, `results`, `decisions` (with `ds.users.issuer`,
`session_id`, `auth_time`), `adjudication`, `audit {head, upgrade_seq, objects}`, `retention`, `licence`,
`previous_packs`, `files`; from 011 `subject.basis` (`census`, or `sample` for a sampled cycle, whose
manifest also carries `sample`, the attested `attests.sample`, and `later_draws`, the ledger's `bottom_k`
draws after it at issue: `{draw_id, spec_name, run_id, cycle, drawn_at, status}`; [SAMPLING.md](SAMPLING.md)
§11.2); from 013, for a format-3 sign-off, `accuracy` `{attested, draws, labelled}`: whether a labelling
is attested (a boolean; the attested one is `facts/attests.json`'s) and the cycle's labellings drawn and
labelled, which the sign-off attests as `labellings` ([ACCURACY.md](ACCURACY.md) §8, §9). Shapes: `claim_text` is §3.4's text (a sampled cycle's, one of the two §3.4 names after it); `issued_by` `{actor, name}`;
`signing_key` `{fingerprint, algorithm}`; `results` `{per_key_results, matched_by_row_hash, counters,
unexplained_critical}`; `decisions` the spec approval, then the sign-off, each `{approval_id, kind,
requested_by, requested_at, steps}`, a step `{step, of, actor, name, issuer, session_id, auth_time, at,
audit_seq}` from its `approval.grant` row; `adjudication` `{attested, draws, scored, sampled}`;
`retention` `{class, days, per_key_detail_until, held, stored_pack}`; `licence` the licence at issue,
`{licence_id, customer, order, installation_id, tier, pairs, starts, ends, state, key}` (`pairs` null
when unlimited, `state` `valid`, `ending` or `grace`; [LICENCE.md](LICENCE.md) §10), or null in a pack
an open build issued, which names no licence, having none (§19; a vendor build's is never null);
`previous_packs` `[{pack_id,
sums_sha256, status, issued_at}]`; `files` each listed file's SHA-256 but its own. `issued_at` is the
build's time, which `ds.packs.issued_at` keeps.

`certificate.pdf` (one `render.Document`, reusing `pdf._counters_grid`) starts `Claim level:
observed`; each section title ends `(observed)`: claim (installation, fingerprint, "compare with your
key register", procedure version, and, in a vendor build's pack only (an open build's claim has no licence
line), `Licence LI-… of Example Bank plc (order ORD-2026-014): enterprise,
2026-10-01 to 2027-09-30, installation I-…`, adding `; issued in its grace period` in grace), subject (a side
whose plan names its engine reads `Side a (legacy): target crm-legacy, APP.CUSTOMERS (oracle 19.21)`, the subject's version: the one the cycle read; where that is not the plan's, the certificate's warnings add `SIDE A READ ON oracle 19.22: the run was planned on 19.21`, and `facts/plan.json` keeps the plan's; a file side reads `Side a (legacy): target legacy-exports, customers
(files duckdb 1.5)`), census, results, classes decided at sign-off, decisions,
adjudication (`draws: 3 (2 scored); attested J-…`; `sampled 50 of 1,204 per-key results; 4,998,916
rows matched by row hash were not sampled`), audit trail, retention (`per-key detail stays in the store
until 2027-10-02; the stored pack is deleted when the cycle is purged: archive this file`; a held run
adds `, or later while run R-… is held` after the date), §3.4, how to verify (VERIFY.txt's lines). A
sampled cycle's starts `Claim level: observed (sampled)`, its census section becomes "Frame (observed)"
(the side lines, then `Frame: N distinct keys (a only …; b only …; both …); U outside key.pattern`), and it
adds "Sample and bounds (observed)" (the draw line naming its audit row, salt label, nonce, both SHA-256
values, the method and the spec approval; the hand check `To check by hand: row N of audit/chain.tsv is
action sample.draw and holds both SHA-256 values, and the SHA-256 of the text <label>:<nonce> is the
salt's`; for `business_code` keys, a line saying that with the nonce anyone holding the pack can tell which
keys it lists lie in a stratum compared whole; the strata table; the reported-only bounds; the contrast counts; `No bound is given for the
strata pooled.`), "Draws of this population (observed)" (the attested earlier draws with their closes,
the later draws at the decision from the final grant, the later draws at issue) and SAMPLING §11.2's
warnings (a side not in the frame, frame keys absent on both sides, earlier draws that did not pass,
later draws incomplete or live at the decision, each later draw failed at issue). A format-3 sign-off's
adds "Labelled accuracy (observed)" after the adjudication: what the attested labelling says
(`accuracy.lines`, whole: its second line counts the cycle's labellings when there are several),
`accuracy.LIMITS` verbatim and the hand check `To check by hand: sha256sum accuracy/labels.csv
accuracy/score.json prints facts/attests.json's accuracy.labels_sha256 and accuracy.score_sha256; row N of
audit/chain.tsv is the accuracy.label row of V-… holding both`. With none attested the section is one
line: `No labelled accuracy is attested: which side is right was not measured.` when no labelling of the
cycle is labelled, else (a token or developer mode labelled one) `No labelled accuracy is attested: 2
labellings of this cycle were drawn (1 labelled), none by a signed-in person; accuracy/attempts.json lists
each with its label counts.`, the counts being `attests.labellings`'. The certificate's warnings gain
ACCURACY §9's: `3 LABELLINGS OF THIS CYCLE WERE DRAWN (2 LABELLED); THE NEWEST A PERSON LABELLED IS
ATTESTED` (more than one drawn), a labeller who started the run or wrote the cited spec approval,
undecidable sampled discrepancies at 5% or more or any not reviewed, a truth source naming a side and a
tokenised key with no path in clear; with none attested and a labelling labelled, only `2 LABELLINGS OF
THIS CYCLE WERE DRAWN (1 LABELLED); NONE WAS LABELLED BY A SIGNED-IN PERSON AND NONE IS ATTESTED`.

### 2.3 Masked, aggregate only

Values appear as stored: tokenised; `business_code` keys and approved `clear` paths in clear. The
builder opens only chat answers, to hash them. **Never in a pack:** raw personal keys, bodies, headers,
blob addresses, scan points, chat questions or answers, notes (but sampling close notes, checked as
revoke reasons are), secrets, targets; nor a sampled key, a key's stratum labels, a per-key u or a frame
key ([SAMPLING.md](SAMPLING.md) §11.5); nor a labelling's keys, observation ids or notes, nor a hash of
its upload: its labels file carries neither the sheet's item numbers nor its order
([ACCURACY.md](ACCURACY.md) §9; what its reader can still confirm is stated below). Audit rows keep staff names and client addresses.

**An MCP agent's rows** ([MCP.md](MCP.md) §9). What an agent's token read of a run is in the run's
segment (§4.3) as any caller's rows are: `key.view {key_masked, cycle, via}` and `mcp.read {tool, calls,
via}`, at most one an hour per caller, tool and run. Its `compare_key` audits `compare_one` and
`run.finished` on the one-shot run it made, not on the run whose key it compared, as the chat's does.
`via` is `mcp:http` or `mcp:stdio`, and `tool` one of three tool names: letters, `_` and `:` only,
so under a key pattern of digits they are no key to the gate below (by the gate's rule; no test of its
own issues such a pack), and under a pattern that admits ordinary words the run gets no pack as it is.
No answer an agent wrote is in a pack: DeltaSleuth stores none and checks none, and `annex/ai_answers.json`
holds the built-in chat's answers alone.

**A file side** ([FILES.md](FILES.md) §13). No pack file holds a matched file's name, inode or own
digest, or any one file's size or times as its pin holds them: the run keeps the pins sealed under its
data key (`params.pinned`), `approvals.plan` leaves `pinned` out, so `facts/plan.json` and `plan_sha256`
hold none of them, and census records, audit rows and warnings name a file by position only. What a pack
does hold of the files is of the side: the count, the bytes of all its files and the newest modification
time, in `facts/plan.json`, the census record and the certificate, and the spec's own `file.path`, in
`facts/plan.json` as in `facts/spec.json`. Of a side of one file those are that file's exact size and
modification time, and its relative path where the spec's path names it. The one digest that leaves is
`files_sha256`, unkeyed so that the
export's owner can reproduce it (`ds target check --manifest`): in `facts/plan.json`, in the manifest's
subject and on the certificate. Whoever holds a pack and the files, or can guess a small export's
content, can confirm that those bytes are what the run compared. The spec's own `file.path` is spec
text, searched by the gate below as `sides.a.table.file.path`: a path holding a number the key pattern
reads as a key (a dated directory under a numeric pattern) gets no pack.

**Raw-value gate.** Hashed files are never masked (that breaks their binding). Before issue,
`pack.raw_values` searches the pack's text files for words that are raw keys of the spec, outside tokens:
known-diff `when`, `table.where`, `value_filter`, request templates, audit texts. A hit refuses (§7.2),
as does a missing `key.pattern` (a table-only spec may omit it) or one that search-matches the empty
string; `business_code` keys are exempt. Such a spec gets no pack whatever its runs hold, and is told so
before it runs: the loader warns `key_pattern_empty` at `/key/pattern` (`rules._key_pattern`:
`key.pattern is missing: a raw key cannot be told from other text, so no evidence pack is issued for a
run of this spec (docs/mvp/PACK.md §2.3); declare a pattern that fits its keys only, or class keys that
are not personal business_code`, or `key.pattern matches the empty string: …`), a warning that leaves the
spec valid, wherever a spec's issues are shown (`ds spec validate`, `POST /v1/specs/validate`, the Specs
screen, a pre-check's draft); and where packs are configured a pair's sign-off precheck lists §7.2's line
beside the sign-off's own problems ([READINESS.md](READINESS.md) §10, `rawvalues.no_pack`), though no
sign-off is refused for it: the pack alone refuses. Hold, erasure and revoke reasons, which segments
carry forever, are checked when written by the same word search (`security/rawvalues.key_shaped`; 422,
CLI exit 2), except under a pattern that cannot tell a key, which only the pack refuses.

A word is a run of characters other than whitespace, quotes (`"`, `'`, `` ` ``), brackets (`( ) [ ] { }
< >`) and `, ; = |`, without its leading and trailing punctuation and symbols (Unicode categories P and
S: `“…”`, `%`, `…` too); its parts are its pieces between `- _ / . : @ + # ~`, and between any characters
but ASCII letters and digits (`id$0712345678`, a zero-width space). A word is a raw key when it, or one of
its parts, normalised by `key.normalize` passes the spec's own key check (`keys.key_matches` with the
compiled `key.pattern`: its anchors, `\A` and `\Z` too, inline flags and search semantics, as when keys
are read); a word counts once. Under `digits_only`, a run of digit groups split by spaces, brackets,
dots, slashes or dashes is judged whole too (`071 234 5601`, `(071) 234-5601`), unless it holds a date.
So `071-234-5601` is a key under `digits_only` and `^[0-9]{10}$`, `gb12345678` under `upper` and
`^[A-Z]{2}[0-9]{8}$`, `071234567801` under the unanchored `[0-9]{10}`, and `2143` in the ticket
`LINK-2143` under `^[0-9]{4,12}$`. Only forms that `key.normalize` turns into a key are recognised: a table
side's `keyMap` forms (`4711` for `ORG-004711`) are not.

DeltaSleuth's own shapes are never keys: a word that wholly is an ISO-8601 time or date, a semantic
version (the schema's `semver`), a DeltaSleuth id (`R-`, `A-`, `D-`, `J-`, `M-`, `N-`, `P-`, `V-`, `LI-`, `SC-` and 26 characters; `U-`,
`W-`, `Q-`, `I-` and the other 16-hex ids, `user:` or `token:` before them; `RC-` and 12 hex), a hash, digest or salt
(16, 32 or 64 lowercase hex digits holding a letter, `sha256:` before them or not) or an IPv4 or IPv6
address, so a key of one of these shapes is not recognised either. Under a pattern that admits short
numbers, a number in generated text, such as the reviewer sheet's `+2 more`, is read as a key; under one
that admits ordinary words (`^[a-z]{3,12}$`), so are the spec's member names, the sheet's headers, the
labels file's headers and label words (`uniform`, `mask`, a rule id), staff names and the builder's
messages. Such a spec gets no pack: the refusal names the first place; tighten
`key.pattern`, or class a key that is not personal `business_code`. Searched: in
`facts/spec.json`, every string and member name but `apiVersion` (a constant) and `key.pattern` (the
gate's own), and as decimal text the integers of inline key lists (`key.source.keys`, nested sources too)
and the numbers of a hash sample's buckets (`values`, `min`, `max`: data values matched against the key or
a frame column, [SAMPLING.md](SAMPLING.md) §11.5);
in the other JSON files, strings that are not numbers (the builder names their members and writes counts,
rates and spec ids as text), but the manifest's `format`, `procedure`, `claim_text`, `installation`,
`signing_key.algorithm`, the licence's `customer`, `order`, `tier` and `state` and a file side's
`sha256_of` in the subject (`_FIXED`); CSV cells that are not numbers; each audit row's strings but its `object` (one
of the pack's ids), not its member names. JSON numbers (counts, times), VERIFY.txt (fixed text, ids and
row numbers) and the certificate (built from the same facts) are not searched. The refusal names the
first place: a spec path in Python's names (`known_diffs[0].when`, `sides.a.table.where`,
`compare.value_filter`, `key.source.keys[0]`), `audit row N`, else the file and where in it
(`facts/attests.json rules[0].when`, `csv/graph.csv line 2`).

**Beyond retention.** A downloaded pack escapes purge and erasure. Its per-key content is masked keys
only, each reversible for low-entropy keys by the tokeniser key (RETENTION §11), and no erasure reaches
them: the attested sheet's ≤ 500 (`score.json`'s disagreements repeat some of them); in
`audit/chain.tsv`, the `key_masked` of each `key.view` row of the run, a row each time a viewer opened a
key or an MCP agent read one (`get_observations`), in any cycle, however many (the rows stay verbatim: the
chain binds them); in
`annex/ai_answers.json`, each answer's `key_masked`, at most one per answer of the cycle. The CSVs carry
none (§2.1). The `accuracy/` files add none: labelling adds only the `key.view` rows of the items its
labellers opened on the Key screen ([ACCURACY.md](ACCURACY.md) §4, §9). `accuracy/labels.csv` carries
neither the sheet's item numbers nor its order (which follows the keys' salted digests, and the pack
holds the salt), so its rows are not tied to keys. What remains: a reader who holds candidate masked keys
(those `key.view` rows, the adjudication sheet) can hash them with the salt and, with `selection_sha256`,
confirm which were drawn, with their population, root cause and `selected_for`; a drawn key's label, basis
and reason then follow wherever the labels file's rows of that population, root cause and `selected_for`
are all alike. No erasure reaches a downloaded pack's copy of them.

**Readiness never enters a pack** ([READINESS.md](READINESS.md) D10): no pack file, manifest field,
certificate line or `VERIFY.txt` step holds a pair's state, a gate, a cycle's measures
(`ds.cycles.readiness`) or `readiness.csv`, and the attests a sign-off records hold none. A pack issued
after its cycle was measured and its pair's state recorded holds, byte for byte, the facts, exports and
audit rows of one issued before (`tests/unit/reports/test_readiness_reports.py`).

**Table pre-checks and the schema guard** ([PRECHECKS.md](PRECHECKS.md) §9) change no pack file: §2.1's
layout is unchanged and no file holds a pre-check, a pre-check's figure or anything of `ds.runs.guard`
(`coverage.csv`, which every pack carries, gains no column from them (file sides added four: a file side's
`rows_rejected_a`, `rows_rejected_b`, `keys_repeated_a` and `keys_repeated_b`, empty for every other
side), and `facts/plan.json` hashes `params`, which
`guard` is not in, so `plan_sha256` is unchanged). `guard` is one mutable record of the run's latest
state, so nothing issued for a cycle is built from it. What a pack holds of the schema guard is the run's
`schema.drift` and `paused` audit rows in its segment (§4.3): append-only, each with its `cycle`, and
counts, closed kinds, ids and 64-hex hashes only, so they pass the raw-value gate under an integer key
pattern (`test_drift_rows_pass_the_pack_gate`, which also holds `coverage.csv` byte for byte with and
without a guard record, and two packs of one cycle issued before and after a later change equal but for
the manifest, the segment, the certificate, `VERIFY.txt` and the checksums that cite them). No `PC-` row
is in a segment.

**A scheduled run** ([SCHEDULES.md](SCHEDULES.md) §5, §7) is packed as any run. Its stored `params`
hold `schedule: {schedule_id, due, revision, time_zone}`, which `approvals.plan` keeps, so
`facts/plan.json` and the `create_run` row of its segment carry it and `plan_sha256` binds it. The
raw-value gate reads the schedule's id and due time as generated text and its zone name as words: a
region's name (`Europe/London`) is letters, `_`, `-` and `/`, while a zone named by its offset
(`Etc/GMT+5`) holds a number that a numeric key pattern matching one or two digits reads as a key, and
such a run gets no pack (`test_scheduled_run_pack_passes_gate`). Saving a schedule warns of every zone
whose name holds a digit, and a preview notes it, without reading the spec's key pattern (SCHEDULES §9,
§16). The schedule's own audit rows are on
object `SC-…`, which no segment selects (§4.3), so none enters a pack; they are links of the whole
chain, which `ds audit verify` checks. A scheduled run's sign-off needs two approvers
([APPROVALS.md](APPROVALS.md) §3).

## 3. Signing and verification

### 3.1 Key and register

```yaml
packs:                                   # needs auth.oidc; absent: pack routes answer 503
  installation: prod-eu-1
  signing_key_ref: file:/run/secrets/pack-signing-key   # PEM PKCS#8, EC P-256, unencrypted
```

- `config.PackSettings`: `packs: needs auth.oidc (packs certify sign-offs, which need SSO)`;
  `packs.signing_key_ref: must not be inside evidence_dir or workspace_dir, whose backups would carry
  it`; `packs.installation: expected 1 to 63 of a-z, 0-9, '.' and '-'`. `security/signing.PackSigner`
  (`ds serve`, `ds packs key|revocations`): `packs.signing_key_ref: expected an unencrypted PEM EC P-256
  private key (docs/mvp/PACK.md §3.1)`, never echoing the file.
- **P-256** (not Ed25519, which needs OpenSSL 3) verifies with `openssl dgst` on OpenSSL 3 and
  LibreSSL, checked in CI (§9); OpenSSL 1.0.2+ and Git for Windows are expected, not checked.
- **Register.** The fingerprint (SHA-256 of the DER SPKI, step 1) is also `system.start`'s `pack_key`.
  `ds packs key --out DIR` writes `fingerprint.txt` (the lines `installation: <name>` and
  `fingerprint: <hex>`) and `procedure.txt` (§3.3), which DEPLOY §3 files in the site's key register at
  key generation, dated. `packs: needs auth.oidc` is checked once the settings load, as `encryption:
  required with auth` is; `ds serve` reads the key before it starts (one `error:` line), and the server's
  `Context.open(packs=True)` keeps it as `ctx.signer`. Custody as the KEK
  (RETENTION §1); one key per installation, never copied to non-production.
- **Rotation** (DEPLOY §3): file a new key, end-date the old entry but keep it, restart;
  decommissioning files the final `revoked.txt`.

### 3.2 What is signed

`SHA256SUMS` (`key.sign(sums, ec.ECDSA(hashes.SHA256(), deterministic_signing=True))`) and
`revoked.txt` (§6), told apart by its first line. The only origin is the register (steps 0–2); certificates of a
site's CA (CMS) are deferred.

### 3.3 Offline verification (`VERIFY.txt`)

Two pack-specific lines, then the canonical procedure `ds packs key` writes, unchanged:

```text
DeltaSleuth evidence pack P-01J… of installation prod-eu-1. Unzip it, cd P-01J…/, then run in sh the copy of the procedure below filed with your key register; its step 0 reads line 2:
PACK=P-01J… SIGNOFF=A-… SPECAPP=A-… HEAD=81234 UPGRADE=5120
# deltasleuth-pack/1 verification
Needs OpenSSL or LibreSSL, POSIX tools, sha256sum (or: sha256sum() { shasum -a 256 "$@"; }).
0. Use the copy filed with your key register; VERIFY.txt must match it from its line 3 (no output):
   sed -n '3,$p' VERIFY.txt | diff - ../FILED-PROCEDURE.txt
   Line 2 names this pack's values; never run it: this checks it and sets them (no output):
   I='[0-9A-Z]{26}'; F="PACK=P-$I SIGNOFF=A-$I SPECAPP=A-$I HEAD=[0-9]+ UPGRADE=[0-9]+"
   L=$(sed -n 2p VERIFY.txt | LC_ALL=C grep -Ex "$F") || echo "line 2 of VERIFY.txt is malformed"
   v() { printf '%s\n' "$L" | cut -d' ' -f"$1" | cut -d= -f2; }
   PACK=$(v 1) SIGNOFF=$(v 2) SPECAPP=$(v 3) HEAD=$(v 4) UPGRADE=$(v 5)
1. The key: prints a fingerprint your register lists for the installation, in service then.
   openssl pkey -pubin -in signing-key.pem -outform DER | sha256sum
2. It signed the list:
   openssl dgst -sha256 -verify signing-key.pem -signature SHA256SUMS.sig SHA256SUMS  -> Verified OK
3. Files unchanged, none added (every line OK; diff prints nothing):
   sha256sum -c SHA256SUMS
   find . -type f | sed 's|^\./||' | LC_ALL=C sort > ../have.txt
   { cut -c67- SHA256SUMS; printf '%s\n' SHA256SUMS SHA256SUMS.sig signing-key.pem; } | LC_ALL=C sort > ../want.txt
   diff ../want.txt ../have.txt
4. Subject: match it with your change record.
   sed -n '/^  "subject"/,/^  }/p' manifest.json
5. Decided (prints "decided" twice, then 1 per line):
   decided() { h=$(sha256sum "$3" | cut -c1-64); awk -F'\t' -v o="$1" -v k="\"$2\": \"$h\"" '$5 == o {
     if ($6 ~ /"action": "approval.revoke"/) r = 1
     if ($6 ~ /"action": "approval.grant"/ && index($6, k) && match($6, /"step": [0-9]+/)) {
       s = substr($6, RSTART + 8, RLENGTH - 8)
       if (match($6, /"of": [0-9]+/) && substr($6, RSTART + 6, RLENGTH - 6) == s) g = 1 } }
     END { print o ": " (g && !r ? "decided" : "NOT DECIDED") }' audit/chain.tsv; }
   decided "$SIGNOFF" attests_sha256 facts/attests.json
   decided "$SPECAPP" content_hash facts/spec.json
   grep -F "\"plan_sha256\": \"$(sha256sum facts/plan.json | cut -c1-64)\"" audit/chain.tsv | grep -c '"action": "create_run"'
   for f in adjudication/sheet.csv adjudication/score.json; do [ ! -f $f ] || grep -c "$(sha256sum $f | cut -c1-64)" facts/attests.json; done
6. Internally consistent: each row hashes to its entry; none is missing between two rows shown for
   one object, or between consecutive seqs (no output):
   T=$(printf '\t'); while IFS="$T" read -r seq prev oprev hash object text || [ -n "$seq" ]; do
     [ "$(printf '%s\n%s\n%s' "$prev" "$oprev" "$text" | sha256sum | cut -c1-64)" = "$hash" ] || echo "row $seq altered"
   done < audit/chain.tsv
   awk -F'\t' -v z="$(printf '%064d' 0)" '{ w = ($5 in l) ? l[$5] : z; if ($3 != w) print "row " $1 ": a row of " $5 " before it is missing"
     if (($1 - 1) in h && $2 != h[$1 - 1]) print "row " $1 ": does not follow row " $1 - 1; l[$5] = $4; h[$1] = $4 }' audit/chain.tsv
7. Unaltered and complete for these objects: export your SIEM's deltasleuth.audit lines with chain.seq
   up to $HEAD, for the objects `cut -f5 audit/chain.tsv | LC_ALL=C sort -u` prints, as "seq<TAB>hash"
   lines to ../siem.tsv (no output):
   awk -F'\t' -v u="$UPGRADE" '$1 > u { print $1 "\t" $4 }' audit/chain.tsv | LC_ALL=C sort > ../pack.tsv
   LC_ALL=C sort ../siem.tsv | diff - ../pack.tsv
   Rows up to $UPGRADE were chained at the upgrade: match each, both ways, with the SIEM line of its id,
   field by field: id, at, actor, actor_name, action, object, detail, request_id, session_id, client_addr
   (at as an instant). Rows written before SIEM lines existed (migration 005) have none: only the chain
   covers them, from the upgrade on:
   awk -F'\t' -v u="$UPGRADE" '$1 <= u' audit/chain.tsv
   The SIEM also holds, ingested at issue time, a pack.issue line for $PACK whose sums_sha256 is:
   sha256sum SHA256SUMS | cut -c1-64
8. Not revoked, with the custodian's latest revoked.txt, revoked.txt.sig and signing-key.pem:
   openssl pkey -pubin -in signing-key.pem -outform DER | sha256sum  -> your register's current fingerprint
   openssl dgst -sha256 -verify signing-key.pem -signature revoked.txt.sig revoked.txt  -> Verified OK
   head -1 revoked.txt  -> # DeltaSleuth pack revocations, key <that fingerprint>, as of <UTC>
                           (your decision date or later; after decommissioning, the final list)
   tail -1 revoked.txt  -> # end
   grep -c "^$PACK " revoked.txt  -> 0
What a valid result proves: certificate.pdf, "What the signature proves".
```

Step 0 makes the filed copy authoritative; no verifier script ships (a forger's would print "ok"). The
auditor runs the filed copy's commands in the pack's folder, never text from the pack: step 0 compares
`VERIFY.txt` from its line 3 with the filed copy, so exactly two lines precede its header, checks line 2
with `grep -Ex` (`P-` and `A-` ids of 26 characters `0-9A-Z`, as the store's checks; digits for `HEAD`
and `UPGRADE`) and cuts the later steps' variables out of it, never running it. Step 8 runs in the
directory holding the custodian's three files, with step 0's variables. The procedure reads output, not
exit codes. Prototyped on macOS (OpenSSL 3.6.2 and coreutils; LibreSSL 3.3.6, `shasum`, BSD awk):
half-approved or revoked sign-offs print `NOT DECIDED`; an edited row fails step 6, a dropped last row
step 7, a forged `VERIFY.txt` step 0 (a line added before its header, or shell in line 2, too), another
key's block with a header line put before the register's in `signing-key.pem` step 2, a listed pack or
renamed `SHA256SUMS` step 8.

`ds packs verify` holds line 2 to the same pattern and the header to line 3. It reads a
`signing-key.pem` (the pack's, and the custodian's for step 8) only as one `PUBLIC KEY` block without
header lines, with nothing but whitespace around it, whose DER is the EC P-256 key's own SPKI
(`signing.load_public`), and fingerprints and verifies with that one key: any other file fails step 1
(step 8: `the list's signing-key.pem is not one PEM EC P-256 public key`). It splits `SHA256SUMS`,
`audit/chain.tsv` and `revoked.txt` at newlines only, as `sh` and `awk` do, never at the U+2028,
U+2029 or U+0085 an audited text may hold raw.

A pack attesting a labelling gets one more line after step 5 (and a sampled pack's `sample draw` line),
`pack_verify._labelled`, the certificate's hand check ([ACCURACY.md](ACCURACY.md) §9): `step 5 labelled
accuracy: ok (row 81230 is labelling V-…'s accuracy.label row and holds the SHA-256 of accuracy/labels.csv
and accuracy/score.json, as facts/attests.json does)`, or `FAILED (…)` when a file is missing or does not
hash to `attests.accuracy`'s value, no `accuracy.label` row of the labelling is in `audit/chain.tsv`, the
row does not hold both hashes, or `facts/attests.json`'s `accuracy` cannot be read (the file does not
parse under a manifest whose `accuracy.attested` is true, or the labelling id or a hash is not text). A
crafted file fails the line and exits 1, never 3. `VERIFY.txt` is
unchanged: the line is `ds packs verify`'s and the certificate's hand check, as a sampled pack's is.

### 3.4 What the signature proves (certificate, verbatim)

"Every statement records what the DeltaSleuth installation <name> observed and stored. The signature
proves that the holder of the key with fingerprint <fp> issued these files unchanged. The audit rows
are unaltered and complete only as checked against your SIEM (step 7) or, on the host, an earlier
recorded head. Nothing here proves the comparison right beyond the recorded facts (adjudication
sampled); agreement outside the compared population, filters, columns and time window, or beyond side
a's census as of its times; true times (the installation's clock); that the key was not stolen or
copied, or history not rewritten before issue by the database owner; that the pack is still valid
(check revocation); or a certified claim, which needs a frozen cut."

A sampled cycle's pack states [SAMPLING.md](SAMPLING.md) §11.3's text instead (`pack.PROVES_SAMPLED`),
naming the frame keys found absent on both sides. That text says the frame's table was read `in one
snapshot` only where it was: for a frame whose census record names a `consistency` other than
`snapshot` (§1.2: a MySQL view, a MySQL table outside InnoDB) it reads `a table in one pass not
observed to be a snapshot` there (`pack.PROVES_SAMPLED_UNPINNED`). `ds packs verify` reads no
`claim_text`: the signature and `SHA256SUMS` bind whichever the pack holds.

## 4. The hash-chained audit log

AUTH §6's deferred chain, simplified: `DSAU` stays; `ds.audit_log` is untouched; no `audit_breaks`.

### 4.1 Definition and writing

Each audit row gets a `ds.audit_chain` row. `text` is `ds.audit_text(row)` (ten columns as jsonb text,
`at` in UTC), **stored**, so a PostgreSQL output change never breaks an old link. `prev` is row
`seq − 1`'s hash, `obj_prev` the object's previous hash (64 zeros first), `hash = hex(SHA-256(prev ‖
"\n" ‖ obj_prev ‖ "\n" ‖ text))`. Per-object links serve step 6; only the SIEM (step 7) shows cuts and
rewrites.

`ds.tg_audit_chain` (`AFTER INSERT`) chains every writer, `psql` included, in commit order under `DSAU`,
held for microseconds as the audit insert comes last (AUTH §6); a stale REPEATABLE READ writer fails on
the primary key. `repo.insert_audit` returns the row with `chain: {seq, hash}`, read back after the
insert (`RETURNING` cannot see the trigger's row; null before 009, as `ds keys migrate` writes rows on
006); SIEM lines and `GET /v1/audit` gain it too (the Admin audit table shows its `seq`). **Anchors:** SIEM lines; `system.start` (`audit_head`:
the head's `{seq, hash}` as it starts; `pack_key`); pack heads; the head `ds audit verify` prints, for a
daily ticket. The SIEM is independent only if it ingests lines at write time, with its own timestamps,
and host audit (DEPLOY §10) shows no ad-hoc `docker exec` emitting them. Archive the `pack.issue` line
or a ticket with the pack: SIEM retention may be shorter.

### 4.2 `ds audit verify [--siem FILE]`

Checks contiguous `seq`, both links, each hash, `text` against the columns, and that every row is
chained, in one REPEATABLE READ snapshot. `text` and the columns are compared in Python, field by field
(`at` as an instant, `client_addr` as the host address), and the entry's `object` with the row's: never
through `ds.audit_text`, which the database owner can replace; a chain row without its audit row fails.
`--siem` compares both ways, by `chain` and content (a line with `chain`, matched by `seq`: its hash and
its ten fields), and backfilled rows (`seq ≤ upgrade_seq`) by content (a line without `chain`, matched by
`id`, its ten fields), over the rows the file's lines span (a SIEM keeps less, and an export precedes
later rows). Two lines of one `seq` or one `id` must say the same (ten fields and `chain`): a line
delivered twice matches. The file's lines end at newlines only: a line may hold U+2028, U+2029 or U+0085
raw. A break exits 2: `error: row 81234 (approval.grant A-…) does not hash to its chain entry: the audit
log was altered after it was written`; else it prints `chain: 81,234 row(s) verified, 1 to 5120 chained
by 009`, with `--siem` `siem: 76,114 line(s) match: rows 5121 to 81234`, then the head, `head: row 81234
<hash>`. Rows are named by `seq`; the first break is reported, the others ending the same way: `row 5 is
missing` (`rows 4 to 5 are`); `row 5 (…) does not follow row 4` (row 1: `does not start the chain`); `…
does not follow row 2, the previous row of R-…`; `… follows a row of R-… that is not in the chain`; `the
audit row with id 7 (…) has no chain entry`; against the SIEM `row 5 (…) differs from its SIEM line`
(backfilled: `row 5 (…), chained at the upgrade, differs …`), `… is in the SIEM but not in the chain` (by
id: `the audit row with id 3 (…) is in the SIEM but not in the log`), `row 5 (…) has no SIEM line: it was
not written by DeltaSleuth, or the SIEM lost it`, `row 5 (…) has two SIEM lines that differ: one is
forged` (by id: `the audit row with id 3 (…) has two SIEM lines …`).

### 4.3 A pack's segment

`repo.audit_segment(conn, objects, head)` reads each object's rows from its first up to `head`: the run,
the cycle's sign-offs (revoked too), the cited spec approval, the lineage's spec versions, the cycle's
adjudications, labellings (`V-…`, from 013: their `accuracy.draw` and `accuracy.label` rows) and earlier
packs, the grants' sessions, and a sampled cycle's attested earlier draws (their
`sample.close` rows; an earlier draw's `sample.draw` row is its own run's, in the segment only when that run
is the pack's, and the certificate cites any other as not in this pack). A broken link refuses: `chain.check_segment`
checks each row's hash, its text against its audit row's columns (in Python), its object's links from
the first and consecutive rows' links, naming the row as §4.2 does. Readiness's rows are on objects no
segment selects (`readiness:<pair>`, `readiness`, and `readiness.config` on the installation's `I-…`),
so none enters a pack; they are links of the whole chain, which `ds audit verify` checks
([READINESS.md](READINESS.md) D11). A table pre-check's rows are on its `PC-…` object, which no segment
selects, and no `PC-` id is in a run's rows, so `rawvalues._GENERATED` is unchanged; the schema guard's
`schema.drift` and `paused` rows are the run's, so a segment holds those up to its head
([PRECHECKS.md](PRECHECKS.md) §9). An MCP caller's `key.view` and `mcp.read` rows are on the run, and
its `compare_key`'s rows on the one-shot run it made, each with `via` in its detail: a segment holds the
run's up to its head ([MCP.md](MCP.md) §9); a role refusal's `authz.denied` is on object `MCP <tool>`,
which no segment selects.

## 5. Adjudication in the store

- **Draw:** `adjudicate.draw(results, limit, salt)` runs P0's `select` with a server salt
  (`secrets.token_hex(16)`), giving the selection (`{candidates, keys, unrepresented}`) and blank sheet
  that `adjudicate.store_draw(ctx, run_id, cycle, *, who, limit=50)` stores in `ds.adjudications`
  (`limit` 1 to 500, else 422 `the adjudication limit must be 1 to 500`; 409 `cycle 2 of run R-… has not
  ended`).
- **Score:** `adjudicate.store_score(ctx, adjudication_id, data, *, who)`: an uploaded `text/csv` (≤ 1
  MiB, else 413 `the reviewed sheet is over 1 MiB`) passes the new `csv.parse_csv(data: bytes, name)`
  (`read_csv`'s body; `name` keeps its messages), P0's `_check_rows` and the new `adjudicate.score_rows`
  (split from `score`, which `cli/main.py` still imports as `score_sheet`). Stored: the sheet as drawn
  with the reviewer's verdicts (upper-cased) and no notes, nothing else a reviewer typed reaching a hashed
  file (§2.3), with bare-hex `sheet_sha256`; the score without notes, P0's `sheet_sha256` renamed
  `upload_sha256` (still `sha256:…`), plus `run_id`, `cycle`, `adjudication_id`, `salt`, and its
  `score_sha256` (sorted-key `jsonio.dumps`); `passed`, `scored_by`. One score per draw.
- **Frozen by the sign-off:** draw and score answer 409 while the cycle has a requested or approved
  sign-off, judged under the cycle's row lock: they take it `FOR UPDATE`, and a request reads it `FOR KEY
  SHARE` before the adjudications, so neither misses the other. The request fixes `attests.adjudication`
  to the newest drawn adjudication of `(run_id, cycle)` scored by a `user:` actor (`{adjudication_id,
  sheet_sha256, score_sha256, adjudicated, agreed, passed}`), or null; rebuilds read that id within
  `(run_id, cycle)`. `ds.approval_authors` excludes the scorer from approving, and the decision says why:
  `approval needs an approver other than you: you scored its adjudication J-…` (APPROVALS §6.2's `<why>`).
- **Purge** nulls the sheet, `selection.keys` and `score.disagreements`; hashes and counts stay.
  `reports/summary.py` reads the adjudication the cycle's requested or approved sign-off attests when the
  P0 directory has none (`… (adjudication J-…, attested by the cycle's sign-off)`).

## 6. Storing, retention, revocation

**Issue** (`reports/pack.issue(ctx, run_id, cycle, *, who)`, a signed-in person): `problems`, build in
memory, then one `repo.audited` transaction: the cycle's row `FOR SHARE` first, no purge started (else
410; a purge starting meanwhile waits for the transaction, then deletes this pack's zip too); the
sign-off `FOR SHARE`, still approved with its `attests_sha256`; insert `ds.packs` and `ds.pack_zips`;
audit `pack.issue` last. Later packs list earlier ones.

**Retention** (amends APPROVALS §2, RETENTION §4.2, §4.3): a purge or erasure after `ds purge release`
revokes the sign-off only when no `issued` pack cites it. `finish_purge` deletes the cycle's zips, sets
`ds.packs.purged_at`, tombstones adjudications; from the purge's start, downloads answer 410 `pack P-…
was purged with cycle 2 on 2027-10-02: its stored copy is deleted; copies downloaded before still
verify` (the date is `purged_at`, or the cycle's `purging_at` while the purge runs). Pack records stay.

**Revocation** comes only with the sign-off: `ds.tg_signoff_packs` revokes a sign-off's packs when it
leaves `approved`, by any writer; `approvals.revoke` audits `pack.revoke` (`repo.revoke_packs`).
`ds packs revocations --out DIR` and `GET /v1/packs/revocations` give `revoked.txt`
(`# DeltaSleuth pack revocations, key <current fingerprint>, as of <UTC>`, then
`P-… <sums_sha256> <revoked_at> <key>` per revoked pack of every key used, then `# end`), its `.sig`
and `signing-key.pem`. Times are `YYYY-MM-DDTHH:MM:SSZ`; packs are listed as revoked, oldest first. The
trigger does the revoking; `repo.revoke_packs(conn, signoff_ids)` reads which packs it revoked, for
`pack.revoke` (`{reason: the sign-off's revoke reason, cascade_of: the sign-off}`, by the revoker).

## 7. Interfaces

### 7.1 API (`api/routes/packs.py`)

| Route | Role | Answer |
|---|---|---|
| `POST /v1/runs/{run_id}/cycles/{cycle}/adjudications` `{limit?: 1..500}` | operator | 201 `{adjudication_id, salt, keys}`; 409 |
| `GET /v1/runs/{run_id}/cycles/{cycle}/adjudications`; `GET /v1/adjudications/{id}/sheet.csv` | viewer | `{items}`; `text/csv` |
| `POST /v1/adjudications/{id}/score` | operator | 200; 409; 413; 422 |
| `POST /v1/runs/{run_id}/cycles/{cycle}/packs` | operator person | 201 `{pack_id, sums_sha256, key_fingerprint}`; 409; 503 |
| `GET /v1/packs?run_id=&status=&cursor=`; `GET /v1/packs/{pack_id}` | viewer | items; one with `manifest` |
| `GET /v1/packs/{pack_id}/download`; `GET /v1/packs/revocations` | viewer | `application/zip`, `no-store`; `pack.download` audited first |

Purged cycles answer 410. `GET /v1/runs/{id}` gains per cycle `census`, `adjudications`, `packs`.

As built (`api/routes/packs.py`): `GET /v1/runs/{id}` adds `cycles`, one `{cycle, census, adjudications,
packs}` per ended cycle, oldest first (`census.of_cycle`'s census, null once the cycle's purge started,
which deletes a scan's state), and `census_pending`, the table sides the current cycle still awaits
(`census.pending`, with `packs` set and the run queued, running or paused; else `[]`). Adjudication items
are the row but `sheet`, which `sheet.csv` serves (`text/csv; charset=utf-8`, `J-….csv`: as drawn, or as
stored once scored); the draw's `keys` are the sheet's masked keys in its order; a score's body is the
upload as sent and its 200 the scored item. Pack items are the row but `manifest`, which the one pack
adds; its record stays readable after the purge. `GET /v1/packs/revocations` is `revocations.zip`,
holding the three files at its root, unaudited. Every route answers the SSO 409 first; only pack routes
answer 503 (an adjudication needs no signing key); an unknown run, adjudication or pack answers 404
(`run R-… not found`); the adjudication list and `sheet.csv` answer 410 for a purged cycle, as draw,
score, issue and download do. `AdjudicationConflict` maps to 409, `UploadTooLarge` to 413 and
`AdjudicationError` (a limit out of range, a sheet that cannot be scored) to 422.

### 7.2 Messages (binding; each asserted)

- 503 `evidence packs are not configured: set packs.signing_key_ref (docs/mvp/PACK.md §3.1)`.
- 409 first on pack and adjudication routes: `evidence packs need SSO (auth.oidc); this deployment runs
  in tokens mode` (or `developer mode`). 403 `issuing a pack needs a signed-in person`.
- 409 on draw and score: `cycle 2 of run R-… has a sign-off (A-…): revoke it before a new adjudication`;
  `adjudication J-… is already scored: draw a new sheet`. 422 on a hold, erasure or revoke reason: `the
  reason holds a key-shaped value: write it without customer keys`.
- 409 at issue, joined by `; `: first the licence's ([LICENCE.md](LICENCE.md) §9; a vendor build's, but
  M8, which an open build words without its last three words), `licence LI-… ended on
  2027-09-30 and its 30 days of grace on 2027-10-30: …` (M4), `evidence packs are an Enterprise feature and
  licence LI-… is pro` (in evaluation `no evidence pack is issued in an unlicensed evaluation: install an
  Enterprise licence for installation I-…`) (M7) and `run R-… was made in an unlicensed evaluation: no
  evidence pack is issued for it; start a new run under the licence` (M8); `run R-… started before evidence
  packs (009): only later runs get a pack`; `cycle 2 of run R-… has no approved sign-off`; `the sign-off A-…
  of run R-… cycle 2 is stale (rules changed since it was signed under attests format 1): revoke it and
  request the sign-off again` (`format 2` and `format 3` likewise) (format 0: `was signed before evidence packs (attests
  format 0)`); APPROVALS §1.2's texts; `cycle 2 of run R-… is not a census: <why>`, `<why>` one of `side a
  (the baseline) was not counted; a count of side b cannot show keys side b lost`, `its keys were given with
  the run and no side is a table to check them against` (or `are the spec's inline list`), `its key set
  lacks 12 key(s) of side a's table legacy.accounts (counted 2026-10-01 08:00 UTC)`, `side a's key census is
  pending` (or `failed (QueryCanceled)`), `side a's files hold 3 line(s) that were not rows` (C1,
  [FILES.md](FILES.md) §11), `its key listing reads target master-data, not side a's`, `graph
  parity (L2) did not run`, and for a sampled cycle `its sampling frame does not enumerate side a (the
  baseline); a frame of side b cannot show keys side b lost` (P1); `the sample D-… of cycle 2 of run R-…
  does not match its sample.draw audit row: no pack` (P2, [SAMPLING.md](SAMPLING.md) §13); `cycle 5 of run
  R-… has no census of its own (last counted 2026-09-01, at cycle 1)`; `cycle 2 of run R-… has no scored
  adjudication in its sign-off A-…: revoke it, draw and score a reviewer sheet, then request the sign-off
  again`; `the attested adjudication J-… did not pass (<reasons>): no pack`; `the pack's texts hold 3 raw
  key-shaped value(s) (known_diffs[2].when): no pack carries them` (or `(audit row 81234)`); `the audit
  trail of run R-… does not verify at row 81234: run ds audit verify`; `the pack would be 131 MiB, over the
  100 MiB limit`; at commit `sign-off A-… was revoked meanwhile`. Also: `spec 12's key.pattern matches the
  empty string: a raw key cannot be told from other text, so no pack`; `spec 12's stored body does not
  re-hash to its content hash (edited, or not storable as uploaded): upload a changed version (a new
  version)`; `the attested adjudication J-… does not hash to its recorded sheet_sha256 and score_sha256: no
  pack`; `the attested labelling V-… does not hash to its recorded labels_sha256 and score_sha256: no pack`
  (P3; also when the segment's `accuracy.label` row of it is missing or does not hold both). The inline
  list's `<why>` reads `its keys are the spec's inline list and no side is a table to
  check them against`; a scan stopped early or cut by maxKeys gets no census `<why>`, as APPROVALS §1.2's
  texts say why. `<reasons>` are the score's `exit.reasons`, joined by `; `. Outside oidc mode the 409 comes
  before the 503.

### 7.3 CLI and UI

| Command | Audit | `DS_OPERATOR` |
|---|---|---|
| `ds packs [--run R-…] [--json]`; `ds packs key --out DIR`; `ds packs revocations --out DIR` | — | no |
| `ds packs download P-… --out DIR` | `pack.download` | yes |
| `ds packs verify FILE.zip --fingerprint HEX [--revocations DIR]`; `ds audit verify [--siem FILE]` | — | no |

Only signed-in people issue (API); break-glass stays revoke-only. `ds packs verify` needs no settings;
refuses a `format` other than `deltasleuth-pack/1`, `..` or absolute paths, symlinks, over 64 entries,
over 200 MiB uncompressed, a ratio over 100:1, a name twice, or a manifest its steps cannot read; runs
steps 0–6 and 8 (0 against its own procedure) and lists step 7's objects, `ok` or `FAILED` per step
(a sampled pack adds `step 5 sample draw: ok|FAILED (…)` after step 5, the certificate's hand check,
failed too when its `facts/attests.json` sample cannot be read; a pack attesting a labelling then adds
`step 5 labelled accuracy: ok|FAILED (…)`, §3.3; and
step 4 prints `basis census|sample` of a pack issued from 011 on: [SAMPLING.md](SAMPLING.md) §11.4, and
after a side's relation its `engine` and `server` when the subject names them, and a file side's `12
file(s) sha256 <64 hex> of the list of its files' SHA-256 values, …`, the subject's `sha256_of` after the
digest ([FILES.md](FILES.md) §16); a pack issued before names none and verifies as it did);
exits 1 on a failure, 2 without `--fingerprint`. It prints `step N <name>: ok (…)`, `… FAILED (<why>)`
or, for steps 4 and 7, what to compare; step 8 needs `--revocations`, whose list must be signed by the
key `--fingerprint` names (after a rotation, check step 8 by hand) and dated no earlier than the pack's
issue (to the second). A refused zip exits 2: `the zip holds 65 entries, over 64: refused`, `the zip's
entry '…' is an absolute path or holds '..': refused`, `… is a symlink: refused`, `… is compressed over
100:1: refused`, `the zip would be 201 MiB uncompressed, over 200 MiB: refused`, `the zip does not hold
one P-… folder and nothing else: refused`, `the zip holds '…' twice: refused` (also two names equal once
case-folded and NFC-normalised, one file on a case-insensitive file system), `the pack's format is '…', not
deltasleuth-pack/1: refused`, `the pack has no readable manifest.json: refused` (also a manifest that is
not an object, or whose `pack_id` is not a string, `issued_at` not a time with its offset, `subject.spec`
not an object or `subject.sides` not an object of objects). `ds packs download` audits `pack.download`
once the file is written, as the CLI's reports do; the API audits it before sending.

**UI:** `Run.tsx` shows per ended cycle the census ("side a counted, 5,000,120 keys, none missing,
2026-09-20; side b not covered"), adjudications (Draw, Download sheet, Upload reviewed sheet), "Issue
evidence pack" and packs (Download). `Approvals.tsx` shows the attested census and adjudication.

As built (`web/src/packs.ts`), a side reads `side a counted, 1,204 keys listed, 2026-09-20` (a listing),
`side a counted, 5,000,120 rows of legacy.accounts, none missing, 2026-09-20` (the key census; `12 key(s)
missing`), `side a counted, 10 rows of public.accounts read to the end in 3 snapshots, 2026-09-20` (a
scan), `side b not covered` or `census of side a failed (QueryCanceled): no pack will cover side a`, joined
by `; `; the live cycle shows `census pending` or that failure. A score reads as the certificate's line
(`50 adjudicated, 50 agreed with the engine: passed`, or `did not pass (<reasons>)`). Draw and Upload
reviewed sheet show under SSO to an operator while the cycle has no requested or approved sign-off, Issue
evidence pack to an operator person once it is signed off (disabled, with the licence's first warning as
the reason, while a vendor build's licence does not allow packs: [LICENCE.md](LICENCE.md) §12), Download sheet and
Download to every viewer until the purge. A review shows `Census: …` and `Adjudication J-…: 50 adjudicated, 50 agreed with the
engine: passed.` (`No adjudication a person scored is attested.`), or for format 0 `Requested before
evidence packs (attests format 0): it attests no census or adjudication.`

### 7.4 Audit actions (added to AUTH §6 and RETENTION §8)

`run.census` (§1.3); `adjudication.draw` (`J-…`; `{run_id, cycle, salt}`); `adjudication.score`
(`{upload_sha256, sheet_sha256, score_sha256, passed}`); `pack.issue` (`P-…`; `{signoff_id, sums_sha256,
manifest_sha256, zip_sha256, key, audit_head, licence_id}`, `licence_id` the licence at issue from 010,
null in an open build, [LICENCE.md](LICENCE.md) §11, §19); `pack.download`; `pack.revoke` (`{reason, cascade_of}`).
Extended: `create_run` (`plan_sha256` of the `params` `repo.create_run` returns via `RETURNING`, minus
`approval`, so floats hash as stored), `system.start` (`audit_head`, `pack_key`), `retention.purged`.
`pack.download`'s detail is `{run_id, cycle}`; `retention.purged` gains `packs`, the packs whose stored
zip the purge deleted; `repo.create_run` returns the stored `params` (`approvals.plan` drops
`approval`).

## 8. Migration `009_packs.sql`

```sql
CREATE FUNCTION ds.audit_text(a ds.audit_log) RETURNS text LANGUAGE sql STABLE AS $$
    SELECT jsonb_build_object('id', a.id, 'at', to_char(a.at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
        'actor', a.actor, 'actor_name', a.actor_name, 'action', a.action, 'object', a.object, 'detail', a.detail,
        'request_id', a.request_id, 'session_id', a.session_id, 'client_addr', host(a.client_addr))::text
$$;
CREATE FUNCTION ds.chain_hash(prev text, obj_prev text, body text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT encode(sha256(convert_to(prev || E'\n' || obj_prev || E'\n' || body, 'UTF8')), 'hex')
$$;
CREATE TABLE ds.audit_chain (
    seq bigint PRIMARY KEY, id bigint NOT NULL UNIQUE REFERENCES ds.audit_log, object text NOT NULL,
    prev text NOT NULL, obj_prev text NOT NULL, hash text NOT NULL UNIQUE, text text NOT NULL,
    CHECK (hash = ds.chain_hash(prev, obj_prev, text))
);
CREATE INDEX audit_chain_object_idx ON ds.audit_chain (object, seq);
CREATE FUNCTION ds.chain_append(r ds.audit_log) RETURNS void LANGUAGE plpgsql AS $$
DECLARE v_seq bigint; v_prev text; v_obj text; v_text text := ds.audit_text(r);
BEGIN
    PERFORM pg_advisory_xact_lock(1146306901);  -- 0x44534155 "DSAU": one chain writer, in commit order
    SELECT seq, hash INTO v_seq, v_prev FROM ds.audit_chain ORDER BY seq DESC LIMIT 1;
    SELECT hash INTO v_obj FROM ds.audit_chain WHERE object = r.object ORDER BY seq DESC LIMIT 1;
    v_prev := coalesce(v_prev, repeat('0', 64));
    v_obj := coalesce(v_obj, repeat('0', 64));
    INSERT INTO ds.audit_chain VALUES (coalesce(v_seq, 0) + 1, r.id, r.object, v_prev, v_obj,
                                       ds.chain_hash(v_prev, v_obj, v_text), v_text);
END $$;
CREATE FUNCTION ds.tg_audit_chain() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN PERFORM ds.chain_append(NEW); RETURN NULL; END $$;
LOCK TABLE ds.audit_log IN SHARE ROW EXCLUSIVE MODE;  -- inserts wait, then are chained
DO $$ DECLARE r ds.audit_log;
BEGIN FOR r IN SELECT * FROM ds.audit_log ORDER BY id LOOP PERFORM ds.chain_append(r); END LOOP; END $$;
CREATE TABLE ds.audit_chain_upgrade AS SELECT coalesce(max(seq), 0) AS upgrade_seq FROM ds.audit_chain;
CREATE TRIGGER audit_log_chain AFTER INSERT ON ds.audit_log FOR EACH ROW EXECUTE FUNCTION ds.tg_audit_chain();
CREATE TRIGGER audit_chain_append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON ds.audit_chain
    FOR EACH STATEMENT EXECUTE FUNCTION ds.append_only();
CREATE TRIGGER audit_chain_upgrade_append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON ds.audit_chain_upgrade
    FOR EACH STATEMENT EXECUTE FUNCTION ds.append_only();
ALTER TABLE ds.runs ADD COLUMN census jsonb;
ALTER TABLE ds.cycles ADD COLUMN census jsonb;
```

The chain SQL ran on `postgres:16-alpine` over 001–008. With the chain's foreign key, `TRUNCATE
ds.audit_log` is refused before its append-only trigger runs (`CASCADE` reaches it). The rest follows
008's style:

- `ds.adjudications`: `adjudication_id`; `(run_id, cycle)` → `ds.cycles`; `salt`; `sample_limit` 1–500;
  `selection`; `sheet`, null exactly once `purged_at` is set; `drawn_by`, `drawn_at`; `upload_sha256`,
  `sheet_sha256`, `score`, `score_sha256`, `passed`, `scored_by`, `scored_at`, all or none. Its trigger
  allows one scoring and one purge UPDATE, and no draw or score during a requested or approved sign-off
  (under the cycle's row lock, §5). `ds.approval_authors` is replaced to add the attested adjudication's
  scorer.
- `ds.packs`: `pack_id`; `(run_id, cycle)`; `signoff_id` → `ds.approvals`; `adjudication_id`;
  `manifest`; unique `sums_sha256`; `zip_sha256`; `key_fingerprint`; `audit_seq`; `issued_by`
  (`user:U-…`), `issued_at`; `status` `issued` or `revoked`, `revoked_by`, `revoked_at`,
  `revoke_reason`; `purged_at`. `ds.tg_packs` refuses DELETE; an `issued` row needs its sign-off
  `approved`, read `FOR SHARE`; an UPDATE only revokes, or sets `purged_at` during the purge.
  `adjudication_id` references `ds.adjudications`, so a plain `TRUNCATE` of it or of `ds.approvals` is
  refused by the foreign key before its append-only trigger (`CASCADE` reaches it).
- `ds.pack_zips (pack_id, zip)`: DELETE only during the purge (its cycle's `purging_at` set); no UPDATE. `ds.tg_signoff_packs` (`AFTER UPDATE OF
  status ON ds.approvals`) revokes a sign-off's packs when it leaves `approved`, copying who, when and
  why. No TRUNCATE; `ds.audit_chain_upgrade` is append-only.

## 9. Tests

No image is pulled (local `postgres:16-alpine`, AUTH's fake IdP). CI sets `DS_REQUIRE_TOOLS=1`: missing
`openssl`, `sha256sum` or `shasum` fails, never skips (Linux: OpenSSL 3, coreutils; macOS: LibreSSL,
`shasum -a 256`).

| File | Behaviours |
|---|---|
| `security/test_signing.py` | RSA, Ed25519, encrypted PEMs refused unechoed; fingerprint equals step 1; a public key file only one bare P-256 block (not a header, two blocks, text around it, a re-encoded key); `VERIFY.txt` equals `ds packs key`'s procedure but its two filled lines |
| `store/test_audit_chain.py`, `test_audit_verify.py` | every writer chained; 4 × 250 concurrent inserts contiguous; stale writer fails; golden `audit_text` in three TimeZones, and `verify` agreeing in each; backfill; caught: edited `detail` or `text`, deleted, unchained or SIEM-only row, swapped `obj_prev`, pre-009 row unlike its line; an edit covered by a replaced `ds.audit_text` (`actor` and `detail` alone, against the SIEM and in a segment; `at` by 1 µs, `client_addr`, `object`, `session_id`); a chain entry without its audit row; a SIEM line unlike its row though its `chain` agrees; two lines of one `seq` or `id` that differ (a line delivered twice matches); a masked `client_addr` raises nothing; U+2028, U+2029 and U+0085 in a SIEM line |
| `runner/test_census.py` | exact merge per key map, composite keys, `where`; out-of-filter substitution refused; timeout warns; key-list soak re-counts; resumed scan reads `3 snapshots`; listing text; a removed target recorded failed, the run failing as without packs |
| `reports/test_pack_problems.py` | every basis and refusal (side b alone, another cycle's census, format 0, a changed field, failed adjudication); draw after request 409; scorer as approver; raw-value canaries in `when`, `where`, `value_filter`, reasons; the spec's own key check (`\A…\Z`, `(?i)`, `(?x)`, unanchored, `digits_only` and `upper` raw forms, 16 digits; in `api/test_approvals.py` and `cli/test_cli.py` a reason under `(?i)` refused 422 and exit 2); DeltaSleuth's shapes and fixed texts never keys; inline integer keys; `^[0-9]{4,12}$` and `^[0-9]{1,10}$` cycles get a pack; a broken audit trail, also a grant edited behind a replaced `ds.audit_text`; concurrent revoke; a purge between build and store (410, no zip left), a download once the purge started; a table-scan run before 009 refused unbuilt |
| `reports/test_pack_contents.py` | facts equal audited hashes; float capabilities; **canary**: raw key, body, header, chat question and answer, notes, KEK, signing key absent, PDF streams decompressed; masked keys only in the sheet, `key.view` rows and answers, none in the CSVs (`reports/test_csv.py`: `keyed=False`); annex this cycle only |
| `reports/test_pack_tamper.py` | **the pack's own `VERIFY.txt` run with `sh`** (`sha256sum`, then `shasum -a 256`) and `ds packs verify`: untouched passes; fail: a flipped byte, also in its sums line; another key; a file added, removed; forged `VERIFY.txt`, a line before its header, shell in its line 2 (never run); another key's block with a header before the register's; re-signed by the test key: edited audit text, last chain row dropped, another cycle's `attests.json`, step 1 of 2 granted, a revoke row; revocation: listed, older list, renamed `SHA256SUMS`, no `# end`, a two-block key; zip: slip, symlink, 65 entries, 201 MiB, 101:1, a name twice (also by case, by NFC), `format` 2, unreadable manifests; U+2028, U+2029 and U+0085 in a hold reason: issued, verified, its SIEM lines read |
| `api/test_packs.py`, `test_adjudications.py` | purge keeps sign-off and pack, drops the zip, 410; an SQL sign-off revoke revokes packs; cascade spec → sign-off → packs; tombstone; each route's SSO 409 before its 503, the token's 403, the refusals; a download verifying away from the host against the served revocations; draw, download, score once, the freeze and each refusal; P0 commands golden-equal (`reports/test_adjudicate.py`) |
| `web/test/packs.test.ts` | the census, score and attested texts; which cycle takes a draw, a score or an issue; the requests, a sheet sent as its bytes |

## 10. Upgrade, limits, deferred

**Upgrade:** the backfill stops the app about a minute per million audit rows. Earlier runs and
format-0 sign-offs (still decided as before) get no pack. From 010 a pack also needs, in a vendor
build, an Enterprise
licence at issue ([LICENCE.md](LICENCE.md) §6); a run without a licence record (made before 010, by
an older build or by an open build) is packed under the licence at issue, and a run made in an unlicensed
evaluation never (D8). An open build asks for no licence, packs its own runs with `licence: null`, and
still refuses a run made in an evaluation (M8; §19).

| Who | Can | How it shows |
|---|---|---|
| Host administrator (key file) | sign any pack, backdated; emit SIEM lines | no `pack.issue` ingested at issue time; host audit; an HSM or KMS (deferred) |
| Database owner, before issue | rewrite log and chain consistently | step 7, but not for a row written before SIEM lines (005) and rewritten before the upgrade |
| Forger without the key | edit any file, `VERIFY.txt` (line 2 too) and `signing-key.pem` too | steps 0–2 |
| Staging copy of the key | look-alike packs | forbidden (DEPLOY §3); the register names the installation |
| Operator | redraw until a sample passes | frozen at request; draws printed |
| Operator | draw labellings until the labels suit, or upload an adverse one through a token | frozen at request; every labelling in `accuracy/attempts.json`; their count attested (`attests.labellings`) and on the certificate, also when none is attested |

**Sampled pairs** are built: [SAMPLING.md](SAMPLING.md) §11; `deltasleuth-pack/1` and
`VERIFY.txt` are unchanged. **Labelled accuracy** is built: [ACCURACY.md](ACCURACY.md) §9; a
format-3 sign-off's pack carries the labelling it attests without keys, under the same format and
`VERIFY.txt`; sign-offs requested before 013 (formats 1 and 2) pack as before. **Deferred:** declared lists, AI answer text, per-key detail; CMS, PAdES,
RFC 3161, HSM or KMS; refusing multi-snapshot scans; direct revoke; CLI issuance;
`verify --store`; a pack page. **Decisions:** D1, a failed attested adjudication refuses; D3, audit inserts serialise on `DSAU`.

**Documents:** APPROVALS §1.2, §2, §8, §12 (and §3, §6.2: the scorer); AUTH §4.1, §4.2 (the routes and
commands), §6, §11; RETENTION §4.2, §4.3, §8, §11, §12; TABLES §5;
DEPLOY §3 (key, register, rotation, decommissioning), §10 (SIEM ingestion, host audit; acceptance:
verify a demo pack away from the host); RUNBOOK; CONTRACTS. **Size:** about 1,500 product, 350
TypeScript and 1,400 test lines, no new dependency.
